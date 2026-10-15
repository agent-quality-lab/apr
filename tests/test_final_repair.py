"""Final repair regression tests (non-network, worker-runnable).

Covers: proxy-disabled no-redirect private HTTP + loopback-only base URLs;
read-only status/stop; serialized serve creation without deadlocking spawned
children; fail-closed DB sidefiles before any SQLite open; chmod failures
surfacing as StoreError; lock/log 0600 enforcement; backup sidecar checks;
atomic import rollback on injected mid-import errors.

Live redirect/proxy/occupied-port/host-behavior tests live in
tests/test_service.py and run on the host (this worker cannot bind loopback).
"""
import io
import json
import os
import stat
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout

import local_service
from local_service import (
    _NoRedirect,
    _private_opener,
    _serve_load_config,
    backup_database,
    cmd_status,
    cmd_stop,
    http_call,
    load_existing_config,
    validate_base_url,
)
from review_store import (
    ImportAbort,
    ReviewStore,
    StoreError,
    secure_existing_private,
    secure_file,
)
from tests.helpers import CARD, REPO_ROOT, fresh_data_dir, full_payload


class PrivateHttpPolicyTest(unittest.TestCase):
    def test_opener_ignores_environment_proxies(self):
        os.environ["http_proxy"] = "http://127.0.0.1:9/"
        os.environ["https_proxy"] = "http://127.0.0.1:9/"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9/"
        self.addCleanup(os.environ.pop, "http_proxy", None)
        self.addCleanup(os.environ.pop, "https_proxy", None)
        self.addCleanup(os.environ.pop, "HTTP_PROXY", None)
        # An explicit empty mapping never consults the environment.
        self.assertEqual(urllib.request.ProxyHandler({}).proxies, {})
        opener = _private_opener()
        # No proxy chain entries exist: http/https go only through the
        # direct connection handlers, even under a hostile proxy env.
        self.assertFalse("proxy" in opener.handle_open)
        for scheme in ("http", "https"):
            kinds = {type(h).__name__ for h in opener.handle_open[scheme]}
            self.assertEqual(kinds, {"HTTPHandler"}
                            if scheme == "http" else {"HTTPSHandler"})

    def test_opener_has_no_following_redirect_handler(self):
        opener = _private_opener()
        followers = [h for h in opener.handlers
                     if isinstance(h, urllib.request.HTTPRedirectHandler)
                     and not isinstance(h, _NoRedirect)]
        self.assertEqual(followers, [])
        self.assertTrue(any(isinstance(h, _NoRedirect)
                            for h in opener.handlers))

    def test_all_redirects_refused_even_loopback(self):
        policy = _NoRedirect()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(policy.redirect_request(
                None, None, code, "Moved",
                {}, "http://127.0.0.1:9999/review/token/"))

    def test_base_url_accepts_exact_loopback_only(self):
        self.assertEqual(validate_base_url("http://127.0.0.1:8080"),
                         "http://127.0.0.1:8080")
        for bad in ("https://127.0.0.1:8080/", "http://localhost:8080/",
                    "http://0.0.0.0:8080/", "http://127.0.0.1:0/",
                    "http://127.0.0.1:99999/",
                    "http://127.0.0.1:8080/extra",
                    "http://127.0.0.1:8080/",
                    "http://evil.example/", "", None, 8080):
            with self.assertRaises(StoreError):
                validate_base_url(bad)

    def test_http_call_rejects_non_loopback_before_network(self):
        seen = []
        original = local_service._PRIVATE_OPENER

        class SpyOpener:
            def open(self, *args, **kwargs):
                seen.append(args)
                raise AssertionError("must not reach the network")

        local_service._PRIVATE_OPENER = SpyOpener()
        self.addCleanup(setattr, local_service, "_PRIVATE_OPENER", original)
        with self.assertRaises(StoreError):
            http_call("http://evil.example/", "/api/internal/ping",
                      credential="secret", payload={"arguments": {}})
        self.assertEqual(seen, [])

    def test_http_call_sends_bearer_only_to_intended_target(self):
        # New transport: one raw HTTPConnection to exact 127.0.0.1:port.
        # The canned connection proves identity with the real HMAC, then
        # the Bearer travels only on that same socket. A hostile proxy env
        # is set to prove the raw connection never consults it.
        import http.client
        import local_service as ls
        credential = "user-credential-pw"
        seen = {"connections": 0, "requests": []}
        real_conn = http.client.HTTPConnection
        real_token = ls.secrets.token_hex
        ls.secrets.token_hex = lambda n: "ab" * n
        self.addCleanup(setattr, ls.secrets, "token_hex", real_token)

        class FakeSock:
            def fileno(self):
                return 7

        class FakeResponse:
            def __init__(self, status, body, will_close=False):
                self.status = status
                self._body = body
                self.will_close = will_close

            def read(self, amt=None):
                return self._body if amt is None else self._body[:amt]

        fake_sock = FakeSock()

        class FakeConnection:
            def __init__(self, host, port=None, timeout=None):
                self.host = host
                self.port = port
                seen["connections"] += 1
                self.sock = None
                self.closed = False

            def connect(self):
                self.sock = fake_sock

            def request(self, method, path, body=None, headers=None):
                seen["requests"].append(
                    {"method": method, "path": path, "headers": dict(headers or {}),
                     "body": body, "sock": self.sock, "auto_open": getattr(self, "auto_open", None)})

            def getresponse(self):
                last = seen["requests"][-1]
                if last["path"] == ls.IDENTITY_PATH:
                    nonce = last["headers"][ls.IDENTITY_NONCE_HEADER]
                    return FakeResponse(200, __import__("json").dumps(
                        {"nonce": nonce,
                         "proof": ls.identity_proof(credential, nonce)}).encode())
                return FakeResponse(200, b'{"ok": true}')

            def close(self):
                self.closed = True

        http.client.HTTPConnection = FakeConnection
        self.addCleanup(setattr, http.client, "HTTPConnection", real_conn)
        os.environ["http_proxy"] = "http://127.0.0.1:9/"
        self.addCleanup(os.environ.pop, "http_proxy", None)
        status, body = http_call("http://127.0.0.1:8080",
                                 "/api/internal/ping",
                                 credential=credential,
                                 payload={"arguments": {}})
        self.assertEqual(status, 200)
        # Exactly one TCP connection for handshake + authenticated request.
        self.assertEqual(seen["connections"], 1)
        self.assertEqual(len(seen["requests"]), 2)
        first, second = seen["requests"]
        self.assertEqual(first["path"], ls.IDENTITY_PATH)
        self.assertNotIn("Authorization", first["headers"])
        self.assertIsNone(first["body"])
        self.assertEqual(second["path"], "/api/internal/ping")
        self.assertEqual(second["headers"]["Authorization"],
                         "Bearer " + credential)
        self.assertIs(second["sock"], fake_sock)
        self.assertIs(first["sock"], fake_sock)
        # The canned connection is the only transport used: no proxy hop
        # exists for the credential to leak through at this layer, and the
        # live redirect-sink proof runs on the host (tests/test_service.py).


class ReadOnlyStatusStopTest(unittest.TestCase):
    def test_status_on_fresh_dir_creates_nothing(self):
        data_dir = fresh_data_dir(self)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cmd_status(data_dir), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(data_dir, "config.json")))
        self.assertFalse(os.path.exists(os.path.join(data_dir, "api_credential")))

    def test_stop_on_fresh_dir_creates_nothing(self):
        data_dir = fresh_data_dir(self)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cmd_stop(data_dir), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(data_dir, "config.json")))
        self.assertFalse(os.path.exists(os.path.join(data_dir, "api_credential")))

    def test_load_existing_config_never_creates(self):
        data_dir = fresh_data_dir(self)
        self.assertEqual(load_existing_config(data_dir), (None, None))
        self.assertEqual(os.listdir(data_dir), [])


class ServeCreationContractTest(unittest.TestCase):
    def test_spawned_child_fails_closed_without_preinitialized_config(self):
        data_dir = fresh_data_dir(self)
        os.environ["APR_SPAWNED_SERVE"] = "1"
        self.addCleanup(os.environ.pop, "APR_SPAWNED_SERVE", None)
        with self.assertRaises(StoreError):
            _serve_load_config(data_dir)
        self.assertEqual(os.listdir(data_dir), [])

    def test_concurrent_cold_loaders_agree_on_one_config(self):
        # The worker sandbox cannot bind loopback, so port selection is
        # mocked: the race under test is config creation through the
        # startup lock, not socket binding. Live host coverage is in
        # tests/test_service.py.
        data_dir = fresh_data_dir(self)
        os.environ.pop("APR_SPAWNED_SERVE", None)
        original_picker = local_service._pick_free_port
        local_service._pick_free_port = lambda: 54321
        self.addCleanup(setattr, local_service, "_pick_free_port",
                        original_picker)
        results, errors = [], []

        def load_once():
            try:
                results.append(_serve_load_config(data_dir))
            except StoreError as exc:  # precise domain failure only
                errors.append(exc)

        threads = [threading.Thread(target=load_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len({port for port, _ in results}), 1)
        self.assertEqual(len({cred for _, cred in results}), 1)


class DbSidefileFailClosedTest(unittest.TestCase):
    def _open_then_close(self, data_dir):
        store = ReviewStore.open(data_dir)
        store.close()

    def test_symlinked_wal_refused_before_sqlite_open(self):
        import review_store
        data_dir = fresh_data_dir(self)
        self._open_then_close(data_dir)
        target = os.path.join(data_dir, "elsewhere-wal")
        with open(target, "wb") as handle:
            handle.write(b"decoy")
        os.symlink(target, os.path.join(data_dir, "reviews.db-wal"))
        calls = []
        real_connect = review_store.sqlite3.connect

        def spy(*args, **kwargs):
            calls.append(args)
            return real_connect(*args, **kwargs)

        review_store.sqlite3.connect = spy
        self.addCleanup(setattr, review_store.sqlite3, "connect", real_connect)
        with self.assertRaises(StoreError):
            ReviewStore.open(data_dir)
        self.assertEqual(calls, [])

    def test_symlinked_db_refused_before_sqlite_open(self):
        import review_store
        data_dir = fresh_data_dir(self)
        calls = []
        real_connect = review_store.sqlite3.connect

        def spy(*args, **kwargs):
            calls.append(args)
            return real_connect(*args, **kwargs)

        review_store.sqlite3.connect = spy
        self.addCleanup(setattr, review_store.sqlite3, "connect", real_connect)
        target = os.path.join(data_dir, "real.db")
        with open(target, "wb") as handle:
            handle.write(b"decoy")
        os.symlink(target, os.path.join(data_dir, "reviews.db"))
        with self.assertRaises(StoreError):
            ReviewStore.open(data_dir)
        self.assertEqual(calls, [])

    def test_tmp_alias_parent_still_opens(self):
        # Data dirs live under the aliased /tmp on macOS; only exact-path
        # symlinks are refused, so a normal open must succeed.
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD))
            self.assertEqual(store.saved_count(), 0)
        finally:
            store.close()

    def test_chmod_failure_surfaces_store_error(self):
        import review_store
        data_dir = fresh_data_dir(self)
        self._open_then_close(data_dir)
        real_chmod = os.chmod

        def flaky_chmod(path, mode):
            if os.path.basename(str(path)).startswith("reviews.db"):
                raise OSError("synthetic permission failure")
            return real_chmod(path, mode)

        review_store.os.chmod = flaky_chmod
        self.addCleanup(setattr, review_store.os, "chmod", real_chmod)
        with self.assertRaises(StoreError):
            ReviewStore.open(data_dir)

    def test_secure_file_chmod_failure_surfaces(self):
        real_chmod = os.chmod
        data_dir = fresh_data_dir(self)
        candidate = os.path.join(data_dir, "cred")
        with open(candidate, "w", encoding="utf-8") as handle:
            handle.write("x")
        try:
            os.chmod = lambda *args: (_ for _ in ()).throw(
                OSError("synthetic"))
            with self.assertRaises(StoreError):
                secure_file(candidate)
        finally:
            os.chmod = real_chmod

    def test_existing_lock_enforced_to_0600(self):
        data_dir = fresh_data_dir(self)
        lock = os.path.join(data_dir, "service.lock")
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.chmod(lock, 0o644)
        secure_existing_private(lock, "service lock")
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)

    def test_symlinked_lock_refused(self):
        data_dir = fresh_data_dir(self)
        target = os.path.join(data_dir, "real-lock")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        link = os.path.join(data_dir, "service.lock")
        os.symlink(target, link)
        with self.assertRaises(StoreError):
            secure_existing_private(link, "service lock")

    def test_backup_sidecar_symlink_refused(self):
        import review_store
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            target = os.path.join(data_dir, "real-wal")
            with open(target, "wb") as handle:
                handle.write(b"decoy")
            backup = os.path.join(data_dir, "backup.db")
            os.symlink(target, backup + "-wal")
            with self.assertRaises(StoreError):
                backup_database(store.db_path, backup)
            self.assertFalse(os.path.exists(backup))
        finally:
            store.close()


class ImportRollbackTest(unittest.TestCase):
    def test_injected_mid_import_error_rolls_back_first_insert(self):
        import review_store
        from tests.helpers import jsonl_record, write_jsonl
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            legacy = os.path.join(data_dir, "legacy.jsonl")
            write_jsonl(legacy, [jsonl_record("rollback-1"),
                                 jsonl_record("rollback-2")])
            real_token = review_store.secrets.token_urlsafe
            calls = []

            def flaky_token(*args, **kwargs):
                calls.append(1)
                if len(calls) > 1:
                    raise RuntimeError("synthetic mid-import failure")
                return real_token(*args, **kwargs)

            review_store.secrets.token_urlsafe = flaky_token
            self.addCleanup(setattr, review_store.secrets,
                            "token_urlsafe", real_token)
            with self.assertRaises(RuntimeError):
                store.import_jsonl(legacy)
            self.assertEqual(store.saved_count(), 0)
            self.assertIsNone(store.get_card_by_review_id("rollback-1"))
            self.assertIsNone(store.get_card_by_review_id("rollback-2"))
        finally:
            store.close()


class ServiceIdentityTest(unittest.TestCase):
    """Same-connection HMAC identity before any Bearer [REDACTED] bytes (non-network).

    The fake connection stands in for the TCP socket: it records every
    request and which socket object carried it, so the tests prove the
    Bearer and review body never leave on an unproven or fresh socket.
    """

    def _fake_transport(self, credential, identity_body=None, identity_status=200,
                        will_close=False, swap_socket=False,
                        swap_on_handshake=False):
        import http.client
        import json as json_module
        import local_service as ls
        seen = {"connections": 0, "requests": []}
        real_conn = http.client.HTTPConnection
        real_token = ls.secrets.token_hex
        ls.secrets.token_hex = lambda n: "ab" * n
        self.addCleanup(setattr, ls.secrets, "token_hex", real_token)

        class FakeSock:
            def fileno(self):
                return 7

        class FakeResponse:
            def __init__(self, status, body, will_close=False):
                self.status = status
                self._body = body
                self.will_close = will_close

            def read(self, amt=None):
                return self._body if amt is None else self._body[:amt]

        first_sock = FakeSock()
        second_sock = FakeSock()

        class FakeConnection:
            def __init__(self, host, port=None, timeout=None):
                self.host = host
                self.port = port
                seen["connections"] += 1
                self.sock = None

            def connect(self):
                self.sock = first_sock

            def request(self, method, path, body=None, headers=None):
                if swap_socket and len(seen["requests"]) >= 1:
                    self.sock = second_sock
                seen["requests"].append(
                    {"method": method, "path": path,
                     "headers": dict(headers or {}), "body": body,
                     "sock": self.sock, "auto_open": getattr(self, "auto_open", None)})

            def getresponse(inner_self):
                last = seen["requests"][-1]
                if last["path"] == ls.IDENTITY_PATH:
                    if identity_body is not None:
                        body = identity_body(last)
                    else:
                        nonce = last["headers"][ls.IDENTITY_NONCE_HEADER]
                        body = json_module.dumps(
                            {"nonce": nonce, "proof": ls.identity_proof(
                                credential, nonce)}).encode()
                    # Model a reconnect racing the handshake: the library
                    # swaps the socket before the client rechecks, so the
                    # pre-send same-socket check must abort before the
                    # secret leaves.
                    if swap_on_handshake:
                        inner_self.sock = second_sock
                    return FakeResponse(identity_status, body,
                                        will_close=will_close)
                return FakeResponse(200, b'{"ok": true}')

            def close(self):
                pass

        http.client.HTTPConnection = FakeConnection
        self.addCleanup(setattr, http.client, "HTTPConnection", real_conn)
        return seen

    def _bearer_bytes(self, seen):
        leaked = b""
        for request in seen["requests"]:
            auth = request["headers"].get("Authorization", "")
            if auth.startswith("Bearer "):
                leaked += auth.encode()
            if request["body"]:
                leaked += request["body"]
        return leaked

    def test_fake_ok_without_proof_sees_no_bearer_or_body(self):
        import json as json_module
        import local_service as ls
        credential = "real-credential-pw"
        # A squatter returning {ok:true} (no nonce/proof) is not our service.
        seen = self._fake_transport(
            credential,
            identity_body=lambda last: json_module.dumps(
                {"ok": True}).encode())
        with self.assertRaises(ConnectionError):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential=credential,
                         payload={"arguments": {"review_id": "synthetic"}})
        self.assertEqual(len(seen["requests"]), 1)
        self.assertEqual(seen["requests"][0]["path"], ls.IDENTITY_PATH)
        self.assertNotIn(b"real-credential-pw", self._bearer_bytes(seen))
        self.assertNotIn(b"synthetic", self._bearer_bytes(seen))

    def test_forged_proof_rejected_without_secret(self):
        import json as json_module
        import local_service as ls
        credential = "real-credential-pw"
        seen = self._fake_transport(
            credential,
            identity_body=lambda last: json_module.dumps(
                {"nonce": last["headers"][ls.IDENTITY_NONCE_HEADER],
                 "proof": "0" * 64}).encode())
        with self.assertRaises(ConnectionError):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential=credential,
                         payload={"arguments": {}})
        self.assertEqual(len(seen["requests"]), 1)
        self.assertNotIn(b"real-credential-pw", self._bearer_bytes(seen))

    def test_close_after_proof_never_reconnects(self):
        import local_service as ls
        credential = "real-credential-pw"
        seen = self._fake_transport(credential, will_close=True)
        with self.assertRaises(ConnectionError):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential=credential,
                         payload={"arguments": {}})
        # Only the handshake left the socket; no second connection and no
        # authenticated request followed the close.
        self.assertEqual(seen["connections"], 1)
        self.assertEqual(len(seen["requests"]), 1)
        self.assertNotIn(b"real-credential-pw", self._bearer_bytes(seen))

    def test_socket_swap_after_proof_never_sends_secret(self):
        import local_service as ls
        credential = "real-credential-pw"
        # A reconnect racing the handshake swaps the socket before the
        # pre-send recheck: the client must abort with only the content-free
        # handshake on the wire. (http.client never swaps mid-send onto a
        # fresh peer; a broken-pipe send fails on the proven socket, which
        # no new listener can read.)
        seen = self._fake_transport(credential, swap_on_handshake=True)
        with self.assertRaises(ConnectionError):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential=credential,
                         payload={"arguments": {}})
        self.assertEqual(len(seen["requests"]), 1)
        self.assertNotIn(b"real-credential-pw", self._bearer_bytes(seen))

    def test_oversized_identity_proof_sends_no_secret(self):
        import local_service as ls
        seen = self._fake_transport("synthetic", identity_body=lambda _: b"x" * 5000)
        with self.assertRaisesRegex(ConnectionError, "proof too large"):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential="synthetic", payload={"arguments": {}})
        self.assertEqual(len(seen["requests"]), 1)
        self.assertEqual(self._bearer_bytes(seen), b"")

    def test_private_transport_disables_automatic_reconnection(self):
        import local_service as ls
        seen = self._fake_transport("synthetic")
        self.assertEqual(ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                                     credential="synthetic", payload={"arguments": {}})[0], 200)
        self.assertEqual(seen["connections"], 1)
        self.assertTrue(all(r["auto_open"] == 0 for r in seen["requests"]))

    def test_every_private_call_reproves_identity(self):
        import local_service as ls
        credential = "real-credential-pw"
        seen = self._fake_transport(credential)
        for _ in range(2):
            status, _ = ls.http_call(
                "http://127.0.0.1:8080", "/api/internal/ping",
                credential=credential, payload={"arguments": {}})
            self.assertEqual(status, 200)
        handshakes = [r for r in seen["requests"]
                      if r["path"] == ls.IDENTITY_PATH]
        self.assertEqual(len(handshakes), 2)
        nonces = [r["headers"][ls.IDENTITY_NONCE_HEADER] for r in handshakes]
        for nonce in nonces:
            self.assertRegex(nonce, r"^[0-9a-f]{32,64}$")


class RatingLockUiContractTest(unittest.TestCase):
    def test_set_busy_locks_all_editable_controls(self):
        import re
        with open(os.path.join(REPO_ROOT, "rating-popup", "app.js"),
                  encoding="utf-8") as handle:
            js = handle.read()
        setter = js[js.index("function setBusy"):]
        setter = setter[:setter.index("\n  }\n") + 5]
        # Save/Skip plus every editable rating control lock synchronously.
        self.assertIn("btnSubmit.disabled", setter)
        self.assertIn("btnSkip.disabled", setter)
        self.assertIn("setEditableDisabled(value)", setter)
        lock = js[js.index("function setEditableDisabled"):]
        lock = lock[:lock.index("\n  }\n") + 5]
        self.assertIn("btnYes.disabled", lock)
        self.assertIn("btnNo.disabled", lock)
        self.assertIn(".chip", lock)
        self.assertIn(".stars button", lock)
        # Busy guards stop programmatic changes even if an event fires.
        self.assertGreaterEqual(len(re.findall(r"if\s*\(\s*busy(?:\s*\|\|\s*formLocked)?\s*\)\s*return",
                                               js)), 2)
        # Failure path restores editing; success path keeps the lock form.
        self.assertIn("if (!value) updateSubmit();", setter)
        self.assertIn("lockForm(doneMessage)", js)
        self.assertIn("formLocked = true", js)
        self.assertIn("btnRetry.disabled = true", setter)


if __name__ == "__main__":
    unittest.main()
