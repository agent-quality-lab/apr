"""Regression tests for controller repair findings 1-9 (non-network).

Real socket/stdio integration tests remain in tests/test_service.py and are
run by the controller on the host; the worker shell cannot bind loopback.
"""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from review_schema import ValidationError, validate_revision
from review_store import ImportAbort, ReviewStore
from tests.helpers import CARD, REPO_ROOT, fresh_data_dir, full_payload

import http.client
import json as _json

import local_service


def legacy_record(review_id="legacy-scope-1", **overrides):
    """Actual old record structure: full review fields plus scope + timestamp."""
    record = dict(CARD)
    record["review_id"] = review_id
    record.update({k: v for k, v in full_payload().items() if k != "review_id"})
    record["review_id"] = review_id
    record["scope"] = "local"
    record["recorded_at"] = "2026-01-02T03:04:05+00:00"
    record.update(overrides)
    return record


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


class LegacyScopeImportTest(unittest.TestCase):
    def test_scope_local_record_imports_with_provenance(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [legacy_record("scope-1"), legacy_record("scope-2")])
            result = store.import_jsonl(legacy)
            self.assertEqual(result["imported"], 2)
            card = store.get_card_by_review_id("scope-1")
            self.assertEqual(card["saved"]["recorded_at"],
                             "2026-01-02T03:04:05+00:00")
            self.assertEqual(card["saved"]["ratings"]["overall"], 5)
            self.assertEqual(card["snapshot"]["task"], CARD["task"])
            self.assertIn("source_digest", card["saved"])
        finally:
            store.close()

    def test_identical_duplicate_rows_in_one_input_ok(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [legacy_record("dup-1"), legacy_record("dup-1")])
            result = store.import_jsonl(legacy)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(store.saved_count(), 1)
        finally:
            store.close()

    def test_conflicting_rows_in_one_input_abort(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            bad = legacy_record("dup-2")
            changed = legacy_record(
                "dup-2", ratings={"overall": 1, "quality": 1,
                                  "speed": 1, "resource_efficiency": 1})
            write_jsonl(legacy, [bad, changed])
            with self.assertRaises(ImportAbort):
                store.import_jsonl(legacy)
            self.assertEqual(store.saved_count(), 0)
        finally:
            store.close()

    def test_conflict_with_pending_job_aborts(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD, review_id="pending-1"))
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [legacy_record("pending-1")])
            with self.assertRaises(ImportAbort):
                store.import_jsonl(legacy)
            self.assertEqual(store.saved_count(), 0)
            card = store.get_card_by_review_id("pending-1")
            self.assertEqual(card["status"], "pending")
        finally:
            store.close()

    def test_conflict_with_saved_job_aborts_repeat_import_idempotent(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [legacy_record("saved-1")])
            self.assertEqual(store.import_jsonl(legacy)["imported"], 1)
            again = store.import_jsonl(legacy)
            self.assertEqual(again["imported"], 0)
            self.assertEqual(store.saved_count(), 1)
            changed = os.path.join(os.path.dirname(store.db_path), "changed.jsonl")
            write_jsonl(changed, [legacy_record(
                "saved-1", ratings={"overall": 1, "quality": 1,
                                    "speed": 1, "resource_efficiency": 1})])
            with self.assertRaises(ImportAbort):
                store.import_jsonl(changed)
            card = store.get_card_by_review_id("saved-1")
            self.assertEqual(card["saved"]["ratings"]["overall"], 5)
        finally:
            store.close()

    def test_unknown_scope_rejected(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [legacy_record("weird-1", scope="cloud")])
            with self.assertRaises(ImportAbort):
                store.import_jsonl(legacy)
            self.assertEqual(store.saved_count(), 0)
        finally:
            store.close()


class BackupApiTest(unittest.TestCase):
    def test_backup_snapshot_readable_during_wal_use(self):
        from local_service import backup_database
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            store.request_job(dict(CARD))
            store.save_rating(full_payload())
            os.makedirs(os.path.join(data_dir, "backups"), mode=0o700,
                        exist_ok=True)
            backup = os.path.join(data_dir, "backups", "reviews-test.db")
            backup_database(store.db_path, backup)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            # Live store keeps working in WAL mode after the backup.
            store.request_job(dict(CARD, review_id="job-002"))
            probe = sqlite3.connect(backup)
            try:
                probe.row_factory = sqlite3.Row
                row = probe.execute(
                    "SELECT * FROM jobs WHERE review_id = 'job-001'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], "saved")
            finally:
                probe.close()
        finally:
            store.close()

    def test_backup_exclusive_creation(self):
        from local_service import backup_database
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            first = os.path.join(data_dir, "b.db")
            backup_database(store.db_path, first)
            with self.assertRaises(FileExistsError):
                backup_database(store.db_path, first)
        finally:
            store.close()


class FrozenSavedJobTest(unittest.TestCase):
    def test_request_job_after_save_keeps_frozen_snapshot(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            token, rev, _ = store.request_job(dict(CARD))
            store.save_rating(full_payload())
            token2, rev2, created = store.request_job(
                dict(CARD, task="Later unrated work"))
            self.assertEqual(token2, token)
            self.assertEqual(rev2, rev)
            self.assertFalse(created)
            card = store.get_card(token)
            self.assertEqual(card["status"], "saved")
            self.assertEqual(card["saved"]["snapshot"]["task"], CARD["task"])
        finally:
            store.close()

    def test_public_card_shows_frozen_snapshot_when_saved(self):
        from local_service import ServiceHandler
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            token, _, _ = store.request_job(dict(CARD))
            store.save_rating(full_payload())
            store.request_job(dict(CARD, task="Later unrated work"))
            card = store.get_card(token)
            public = ServiceHandler._public_card(card)
            self.assertEqual(public["snapshot"]["task"], CARD["task"])
            self.assertEqual(public["saved"]["ratings"]["overall"], 5)
        finally:
            store.close()


class BrowserRevisionTest(unittest.TestCase):
    def test_browser_payload_requires_explicit_revision(self):
        from local_service import validate_browser_payload
        card = {"revision": 3, "snapshot": dict(CARD)}
        with self.assertRaises(ValidationError):
            validate_browser_payload({}, card)
        with self.assertRaises(ValidationError):
            validate_browser_payload({"completion": "yes", "ratings": {}}, card)
        with self.assertRaises(ValidationError):
            validate_browser_payload({"revision": True, "ratings": {}}, card)
        with self.assertRaises(ValidationError):
            validate_browser_payload({"revision": "3", "ratings": {}}, card)
        parsed = validate_browser_payload(
            {"completion": "yes", "ratings": full_payload()["ratings"],
             "revision": 3}, card)
        self.assertEqual(parsed["revision"], 3)

    def test_validate_revision_rejects_bool_and_malformed(self):
        for bad in (True, False, 0, -1, "2", 2.0, None):
            with self.assertRaises(ValidationError):
                validate_revision(bad)


class ConfigSafetyTest(unittest.TestCase):
    def test_malformed_config_raises_recovery_error_without_rewrite(self):
        from local_service import load_or_create_config
        from review_store import StoreError
        data_dir = fresh_data_dir(self)
        os.makedirs(data_dir, exist_ok=True)
        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"port": "not-a-port"}))
        with open(config_path, encoding="utf-8") as handle:
            before = handle.read()
        with self.assertRaises(StoreError):
            load_or_create_config(data_dir)
        with open(config_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_symlinked_sensitive_paths_refused(self):
        from local_service import load_or_create_config
        from review_store import StoreError, ensure_private_dir
        real = fresh_data_dir(self)
        # Unique child inside our own temporary directory: never touch or
        # unlink an unrelated preexisting file.
        probe_dir = tempfile.mkdtemp(prefix="apr-probe-")
        self.addCleanup(shutil.rmtree, probe_dir, True)
        link = os.path.join(
            probe_dir, f"apr-symlink-probe-{os.getpid()}")
        os.symlink(real, link)
        with self.assertRaises(StoreError):
            ensure_private_dir(link)
        with self.assertRaises(StoreError):
            load_or_create_config(link)


class StopSafetyTest(unittest.TestCase):
    def test_stale_pid_never_kills_unrelated_process(self):
        from local_service import cmd_stop
        data_dir = fresh_data_dir(self)
        os.makedirs(data_dir, exist_ok=True)
        decoy = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(60)"])
        self.addCleanup(decoy.wait, 15)
        try:
            with open(os.path.join(data_dir, "service.pid"), "w",
                      encoding="utf-8") as handle:
                json.dump({"pid": decoy.pid, "port": 1}, handle)
            # Port 1 cannot host our service; authenticated shutdown fails,
            # so the stale pid file must not authorize a kill.
            self.assertEqual(cmd_stop(data_dir), 0)
            self.assertIsNone(decoy.poll())
        finally:
            decoy.terminate()


class _FakeSock:
    def fileno(self):
        return 5


class _FakeProofResponse:
    def __init__(self, body, status=200, will_close=False):
        self._body = body
        self.status = status
        self.will_close = will_close
        self.read_size = None

    def read(self, amt=None):
        self.read_size = amt
        if amt is None:
            return self._body
        return self._body[:amt]


class _FakeFinalResponse:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self._body = body
        self.will_close = False

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]


class _FakeConnection:
    """Non-network stand-in for http.client.HTTPConnection.

    Never opens a socket; serves one queued proof response and one final
    response so http_call's same-socket/auto_open/bound logic runs fully.
    """
    instances = []

    def __init__(self, host, port, timeout=None, proof_body=None,
                fail_on=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_open = 1
        self.sock = None
        self.requests = []
        self._proof_body = proof_body
        self._fail_on = fail_on or {}
        _FakeConnection.instances.append(self)

    def connect(self):
        if self._fail_on.get("connect"):
            raise self._fail_on["connect"]
        self.sock = _FakeSock()

    def request(self, method, path, body=None, headers=None):
        if self._fail_on.get("request"):
            raise self._fail_on["request"]
        self.requests.append({"method": method, "path": path,
                              "headers": dict(headers or {})})

    def getresponse(self):
        if self._fail_on.get("getresponse"):
            raise self._fail_on["getresponse"]
        if not getattr(self, "_proof_served", False):
            self._proof_served = True
            if self._proof_body == "__valid__":
                nonce = self.requests[-1]["headers"].get(
                    local_service.IDENTITY_NONCE_HEADER, "")
                doc = {"nonce": nonce,
                       "proof": local_service.identity_proof("cred-x", nonce)}
                return _FakeProofResponse(_json.dumps(doc).encode())
            return _FakeProofResponse(self._proof_body)
        return _FakeFinalResponse()

    def close(self):
        pass


class IdentityTransportTest(unittest.TestCase):
    """http_call identity transport: no loopback sockets are opened here.

    The fake connection above replaces http.client.HTTPConnection, so every
    assertion runs against http_call's real handshake/same-socket/bound
    logic without any network use.
    """
    BASE = "http://127.0.0.1:9"

    def _patch(self, **fake_kwargs):
        real = http.client.HTTPConnection
        _FakeConnection.instances.clear()

        def factory(host, port, timeout=None):
            return _FakeConnection(host, port, timeout=timeout,
                                   **fake_kwargs)
        http.client.HTTPConnection = factory
        self.addCleanup(setattr, http.client, "HTTPConnection", real)
        return _FakeConnection

    def test_handshake_disables_auto_open_and_bounds_proof_read(self):
        self._patch(proof_body="__valid__")
        status, _ = local_service.http_call(
            self.BASE, "/api/internal/ping", credential="cred-x",
            payload={"arguments": {}}, timeout=5)
        self.assertEqual(status, 200)
        conn = _FakeConnection.instances[-1]
        self.assertEqual(conn.auto_open, 0)
        # The secret must ride the proven connection, not a reconnect.
        authed = [r for r in conn.requests if "Authorization" in r["headers"]]
        self.assertEqual(len(authed), 1)

    def test_oversized_proof_rejected_before_secret_leaves(self):
        self._patch(proof_body=b"x" * 5000)
        with self.assertRaises(ConnectionError):
            local_service.http_call(
                self.BASE, "/api/internal/ping", credential="cred-x",
                payload={"arguments": {}}, timeout=5)
        conn = _FakeConnection.instances[-1]
        authed = [r for r in conn.requests if "Authorization" in r["headers"]]
        self.assertEqual(authed, [])

    def test_http_exception_maps_to_connection_error(self):
        self._patch(proof_body="__valid__",
                    fail_on={"connect": http.client.BadStatusLine("bad")})
        with self.assertRaises(ConnectionError):
            local_service.http_call(
                self.BASE, "/api/internal/ping", credential="cred-x",
                payload={"arguments": {}}, timeout=5)
        self._patch(proof_body="__valid__",
                    fail_on={"getresponse": http.client.NotConnected()})
        with self.assertRaises(ConnectionError):
            local_service.http_call(
                self.BASE, "/api/internal/ping", credential="cred-x",
                payload={"arguments": {}}, timeout=5)


class FormLockContractTest(unittest.TestCase):
    """Popup form-lock contract (static, non-network).

    Rendered click-through stays controller-owned; these assertions pin the
    regression fix: a terminal locked state that busy/restore paths respect,
    a Retry that can never unlock or interrupt, and a 409-to-saved path that
    keeps its read-only confirmation without generic retry feedback.
    """
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "rating-popup", "app.js"),
                  encoding="utf-8") as handle:
            cls.js = handle.read()

    def test_terminal_lock_state_set_by_lock_form(self):
        self.assertIn("var formLocked = false", self.js)
        self.assertIn("formLocked = true", self.js)

    def test_busy_and_submit_respect_terminal_lock(self):
        self.assertIn("if (busy || formLocked || !cardLoaded) return;",
                      self.js)
        self.assertIn("function setBusy(value, label) {\n"
                      "    if (formLocked) {", self.js)
        self.assertIn("function updateSubmit() {\n"
                      "    if (formLocked) {", self.js)

    def test_busy_hides_retry_and_retry_never_unlocks(self):
        self.assertIn('btnRetry.addEventListener("click", function () {\n'
                      "    if (busy || formLocked) return;", self.js)

    def test_conflict_saved_path_keeps_readonly_confirmation(self):
        self.assertIn("await loadCard();\n"
                      "          if (formLocked) {", self.js)
        self.assertIn("} catch (error) {\n"
                      "      if (formLocked) {", self.js)
        self.assertNotIn("lockForm(null)", self.js)

    def test_saved_state_hides_unsaved_selections(self):
        self.assertIn('document.querySelector(".binary-block").hidden = true;',
                      self.js)
        self.assertIn("reasonsBlock.hidden = true;", self.js)
        self.assertIn("ratingsBlock.hidden = true;", self.js)


if __name__ == "__main__":
    unittest.main()
