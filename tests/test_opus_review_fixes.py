"""Opus PR#1 robustness fixes: auth comparison, migrations, import txn, summary filter.

Non-network only: the worker shell cannot bind loopback. Real socket/stdio
integration runs on the host (controller).
"""
import http.client
import json as json_module
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest

import local_service as ls
from review_schema import ValidationError
from review_store import ImportAbort, ReviewStore, StoreError
from tests.helpers import CARD, REPO_ROOT, fresh_data_dir, full_payload, jsonl_record, write_jsonl


def fake_identity_transport(testcase, identity_body):
    """Fake HTTPConnection serving one canned identity proof (no network)."""
    seen = {"connections": 0, "requests": []}
    real_conn = http.client.HTTPConnection
    real_token = ls.secrets.token_hex
    ls.secrets.token_hex = lambda n: "ab" * n
    testcase.addCleanup(setattr, ls.secrets, "token_hex", real_token)

    class FakeSock:
        def fileno(self):
            return 7

    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body
            self.will_close = False

        def read(self, amt=None):
            return self._body if amt is None else self._body[:amt]

    sock = FakeSock()

    class FakeConnection:
        def __init__(self, host, port=None, timeout=None):
            seen["connections"] += 1
            self.sock = None

        def connect(self):
            self.sock = sock

        def request(self, method, path, body=None, headers=None):
            seen["requests"].append(
                {"method": method, "path": path,
                 "headers": dict(headers or {}), "body": body})

        def getresponse(self):
            last = seen["requests"][-1]
            assert last["path"] == ls.IDENTITY_PATH
            return FakeResponse(200, identity_body(last))

        def close(self):
            pass

    http.client.HTTPConnection = FakeConnection
    testcase.addCleanup(setattr, http.client, "HTTPConnection", real_conn)
    return seen


def handler_stub(store, body):
    """ServiceHandler without a socket: stub server/headers/io (no network)."""
    handler = ls.ServiceHandler.__new__(ls.ServiceHandler)
    handler.server = type("Server", (), {
        "credential": "synthetic-credential", "store": store,
        "service_port": 1})()
    handler.headers = {"Authorization": "Bearer synthetic-credential"}
    captured = {}
    handler.send_json = lambda status, obj: captured.update(
        status=status, obj=obj)
    handler.send_error = lambda *a, **k: captured.update(
        status=a[0] if a else None)
    handler.read_json_body = lambda limit: body
    return handler, captured


class NonAsciiAuthTest(unittest.TestCase):
    def test_non_ascii_proof_is_connection_error_without_secret(self):
        credential = "synthetic-credential-pw"

        def body(last):
            nonce = last["headers"][ls.IDENTITY_NONCE_HEADER]
            return json_module.dumps(
                {"nonce": nonce, "proof": "é" * 64}).encode()

        seen = fake_identity_transport(self, body)
        with self.assertRaises(ConnectionError):
            ls.http_call("http://127.0.0.1:8080", "/api/internal/ping",
                         credential=credential,
                         payload={"arguments": {"review_id": "synthetic"}})
        # Only the content-free handshake left; no Bearer, no review body.
        self.assertEqual(len(seen["requests"]), 1)
        for request in seen["requests"]:
            self.assertNotIn("Authorization", request["headers"])
            self.assertIsNone(request["body"])

    def test_non_ascii_authorization_is_401_false(self):
        handler, _ = handler_stub(None, None)
        handler.headers = {"Authorization": "Bearer évil"}
        self.assertFalse(handler.bearer_ok())

    def test_ascii_auth_still_compared_constant_time(self):
        handler, _ = handler_stub(None, None)
        handler.headers = {"Authorization": "Bearer synthetic-credential"}
        self.assertTrue(handler.bearer_ok())
        handler.headers = {"Authorization": "Bearer wrong"}
        self.assertFalse(handler.bearer_ok())


class MigrationTransactionTest(unittest.TestCase):
    def test_injected_migration_failure_rolls_back_and_retry_succeeds(self):
        import review_store
        data_dir = fresh_data_dir(self)
        real_migrations = review_store.MIGRATIONS
        review_store.MIGRATIONS = real_migrations + [
            (99, "CREATE TABLE mig_probe_x (x TEXT);"
                 " INSERT INTO no_such_table_xyz VALUES (1);")]
        self.addCleanup(setattr, review_store, "MIGRATIONS", real_migrations)
        with self.assertRaises(StoreError):
            ReviewStore.open(data_dir)
        # DDL and version marker rolled back: probe table absent, 99 untracked.
        probe = sqlite3.connect(data_dir + "/reviews.db")
        try:
            tables = {row[0] for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            probe.close()
        self.assertNotIn("mig_probe_x", tables)
        # Retry on the same directory converges (no leaked lock/connection).
        review_store.MIGRATIONS = real_migrations
        store = ReviewStore.open(data_dir)
        try:
            self.assertEqual(store.migration_versions(), [1, 2, 3, 4])
        finally:
            store.close()

    def test_concurrent_cold_opens_converge_without_duplicates(self):
        data_dir = fresh_data_dir(self)
        barrier = threading.Barrier(8)
        errors = []

        def open_once():
            try:
                barrier.wait(timeout=30)
                store = ReviewStore.open(data_dir)
                try:
                    store.request_job(dict(CARD))
                finally:
                    store.close()
            except Exception as exc:  # noqa: BLE001 - collected, then failed
                errors.append(exc)

        threads = [threading.Thread(target=open_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        store = ReviewStore.open(data_dir)
        try:
            self.assertEqual(store.migration_versions(), [1, 2, 3, 4])
            count = store._conn.execute(
                "SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, 4)
        finally:
            store.close()


class ImportTransactionTest(unittest.TestCase):
    def test_concurrent_domain_conflict_aborts_cleanly_never_raw(self):
        import review_store
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            legacy = os.path.join(data_dir, "legacy.jsonl")
            write_jsonl(legacy, [jsonl_record("race-1")])
            real_token = review_store.secrets.token_urlsafe
            calls = []

            def rival_token(*args, **kwargs):
                # A second writer commits a conflicting pending row for the
                # same review_id between the import's plan and its apply.
                if not calls:
                    calls.append(1)
                    store._conn.execute(
                        "INSERT INTO jobs (review_id, job_token, snapshot,"
                        " revision, status, updated_at)"
                        " VALUES (?, ?, ?, 1, 'pending', ?)",
                        ("race-1", "rival-token", json_module.dumps(
                            {"review_id": "race-1"}), "2026-01-01T00:00:00+00:00"))
                return real_token(*args, **kwargs)

            review_store.secrets.token_urlsafe = rival_token
            self.addCleanup(setattr, review_store.secrets,
                            "token_urlsafe", real_token)
            with self.assertRaises(ImportAbort):
                store.import_jsonl(legacy)
            # Atomic: no partial saved data from the aborted import.
            self.assertEqual(store.saved_count(), 0)
            card = store.get_card_by_review_id("race-1")
            self.assertTrue(card is None or card["saved"] is None)
        finally:
            store.close()

    def test_two_connection_race_never_leaks_integrity_error(self):
        data_dir = fresh_data_dir(self)
        store_a = ReviewStore.open(data_dir)
        store_b = ReviewStore.open(data_dir)
        try:
            legacy = os.path.join(data_dir, "race.jsonl")
            write_jsonl(legacy, [jsonl_record("duel-1")])
            errors = []
            stop = threading.Event()

            def rival():
                while not stop.is_set():
                    try:
                        store_b.request_job(dict(CARD, review_id="duel-1"))
                        return
                    except StoreError:
                        return
                    except Exception as exc:  # noqa: BLE001 - must be StoreError
                        errors.append(exc)
                        return

            thread = threading.Thread(target=rival)
            thread.start()
            try:
                store_a.import_jsonl(legacy)
            except ImportAbort:
                pass
            except Exception as exc:  # noqa: BLE001 - only ImportAbort is clean
                errors.append(exc)
            finally:
                stop.set()
            thread.join(30)
            self.assertEqual(errors, [])
            # Exactly one winner, no partial data: pending or saved, never both.
            card = store_a.get_card_by_review_id("duel-1")
            self.assertIsNotNone(card)
            self.assertIn(card["status"], ("pending", "saved"))
            if card["status"] == "saved":
                self.assertIsNotNone(card["saved"])
        finally:
            store_a.close()
            store_b.close()


class SummaryFilterTest(unittest.TestCase):
    def test_invalid_job_type_rejected_before_query(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD))
            store.save_rating(full_payload())
            for bad in ("codng", "", 123, True, ["coding"], {"job_type": "coding"}):
                with self.assertRaises(ValidationError, msg=repr(bad)):
                    store.summary(bad)
            # Omitted filter still returns everything.
            self.assertEqual(store.summary()["count"], 1)
            self.assertEqual(store.summary(None)["count"], 1)
            self.assertEqual(store.summary("coding")["count"], 1)
        finally:
            store.close()

    def test_summary_endpoint_returns_400_for_invalid_filter(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            for bad in ("codng", 123, ["coding"], {"job_type": "coding"}):
                handler, captured = handler_stub(
                    store, {"arguments": {"job_type": bad}})
                handler._handle_internal("summary")
                self.assertEqual(captured.get("status"), 400, repr(bad))
            handler, captured = handler_stub(store, {"arguments": {}})
            handler._handle_internal("summary")
            self.assertEqual(captured.get("status"), 200)
            self.assertEqual(captured["obj"]["count"], 0)
            handler, captured = handler_stub(
                store, {"arguments": {"job_type": "coding"}})
            handler._handle_internal("summary")
            self.assertEqual(captured.get("status"), 200)
        finally:
            store.close()


class ColdOpenInitLockTest(unittest.TestCase):
    """Concurrent fresh opens/closes converge via the per-directory init lock.

    Regression for the cold-open race where pre-open sidefile checks and
    SQLite connect/PRAGMA setup ran outside any cross-process lock:
    `OperationalError: database is locked` from `PRAGMA journal_mode=WAL`
    in `__init__`, and `StoreError` when a `-journal` sidecar vanished
    between the existence check and `chmod`. Each iteration uses a fresh
    synthetic directory with an 8-way concurrent open/close burst.
    """

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def _assert_private_layout(self, data_dir):
        self.assertEqual(self._mode(data_dir), 0o700)
        lock = os.path.join(data_dir, "init.lock")
        self.assertTrue(os.path.isfile(lock))
        self.assertFalse(os.path.islink(lock))
        self.assertEqual(self._mode(lock), 0o600)
        db = os.path.join(data_dir, "reviews.db")
        self.assertEqual(self._mode(db), 0o600)
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = db + suffix
            if os.path.lexists(candidate):
                self.assertFalse(os.path.islink(candidate))
                if os.path.isfile(candidate):
                    self.assertEqual(self._mode(candidate), 0o600,
                                     candidate)

    def test_repeated_concurrent_cold_open_close_converges(self):
        for _ in range(5):
            data_dir = fresh_data_dir(self)
            barrier = threading.Barrier(8)
            errors = []

            def open_close_once():
                try:
                    barrier.wait(timeout=30)
                    store = ReviewStore.open(data_dir)
                    try:
                        self.assertEqual(store.migration_versions(), [1, 2, 3, 4])
                    finally:
                        store.close()
                except Exception as exc:  # noqa: BLE001 - collected, then failed
                    errors.append(exc)

            threads = [threading.Thread(target=open_close_once)
                       for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            for thread in threads:
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            store = ReviewStore.open(data_dir)
            try:
                self.assertEqual(store.migration_versions(), [1, 2, 3, 4])
                count = store._conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                self.assertEqual(count, 4)
            finally:
                store.close()
            self._assert_private_layout(data_dir)

class CrossProcessLockTest(unittest.TestCase):
    def _probe_lock(self, db_path):
        probe = """import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 try:
  fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  print('acquired')
 except BlockingIOError:print('blocked')
finally:os.close(fd)
"""
        return subprocess.check_output(
            [sys.executable, "-c", probe, db_path], text=True,
            cwd=REPO_ROOT, timeout=10).strip()

    def test_sqlite_locks_survive_second_same_process_store(self):
        data_dir = fresh_data_dir(self)
        first = ReviewStore.open(data_dir)
        second = None
        try:
            self.assertEqual(self._probe_lock(first.db_path), "blocked")
            second = ReviewStore.open(data_dir)
            self.assertEqual(self._probe_lock(first.db_path), "blocked")
            second.close()
            second = None
            self.assertEqual(self._probe_lock(first.db_path), "blocked")
        finally:
            if second is not None:
                second.close()
            first.close()

    def test_other_process_close_preserves_live_wal_and_later_save(self):
        data_dir = fresh_data_dir(self)
        first = ReviewStore.open(data_dir)
        try:
            first.request_job(dict(CARD))
            child = """import sys
from review_store import ReviewStore
store=ReviewStore.open(sys.argv[1]);store.close()
"""
            subprocess.run([sys.executable, "-c", child, data_dir],
                           cwd=REPO_ROOT, check=True, timeout=15)
            self.assertTrue(os.path.isfile(first.db_path + "-wal"))
            first.save_rating(full_payload())
            reader = """import json,sys
from review_store import ReviewStore
store=ReviewStore.open(sys.argv[1])
try:print(json.dumps({'count':store.saved_count(),'status':store.get_card_by_review_id(sys.argv[2])['status']}))
finally:store.close()
"""
            visible = json_module.loads(subprocess.check_output(
                [sys.executable, "-c", reader, data_dir, CARD["review_id"]],
                cwd=REPO_ROOT, text=True, timeout=15))
            self.assertEqual(visible, {"count": 1, "status": "saved"})
        finally:
            first.close()


if __name__ == "__main__":
    unittest.main()
