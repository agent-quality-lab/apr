#!/usr/bin/env python3
"""SQLite-backed review store: migrations, transactions, legacy JSONL import.

Standard library only. Depends on review_schema for validation (no cycles:
review_schema imports nothing local). Sensitive files are never symlinks and
stay mode 0600 inside a mode 0700 directory.
"""
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from review_schema import JOB_TYPES, ValidationError, validate_review

SCOPE = "local-only: this machine's review database; not community or benchmark data"

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    review_id TEXT PRIMARY KEY,
    job_token TEXT UNIQUE NOT NULL,
    snapshot TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    saved_snapshot TEXT,
    saved_full TEXT,
    saved_at TEXT,
    updated_at TEXT NOT NULL
);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS import_provenance (
    source_digest TEXT NOT NULL,
    review_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source_digest, review_id)
);
"""

MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS runtime_bindings (
    runtime TEXT NOT NULL,
    installation_key TEXT NOT NULL,
    native_chat_key TEXT NOT NULL,
    identity_source TEXT NOT NULL,
    review_id TEXT NOT NULL UNIQUE REFERENCES jobs(review_id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    start_source TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    PRIMARY KEY (runtime, installation_key, native_chat_key)
);
CREATE TABLE IF NOT EXISTS runtime_events (
    event_key TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES jobs(review_id),
    turn_key TEXT,
    kind TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    observed_monotonic_ns INTEGER NOT NULL,
    clock_epoch TEXT,
    model_slug TEXT,
    safe_tool_category TEXT,
    tool_key TEXT,
    source TEXT,
    adapter_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_events_review_turn
    ON runtime_events(review_id, turn_key, kind);
CREATE TABLE IF NOT EXISTS runtime_turns (
    review_id TEXT NOT NULL REFERENCES jobs(review_id),
    turn_key TEXT NOT NULL,
    first_observed_at TEXT,
    terminal_observed_at TEXT,
    terminal_kind TEXT,
    first_model TEXT,
    last_model TEXT,
    coverage TEXT NOT NULL,
    PRIMARY KEY (review_id, turn_key)
);
CREATE TABLE IF NOT EXISTS runtime_metadata (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_gaps (
    review_id TEXT NOT NULL REFERENCES jobs(review_id),
    gap_key TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (review_id, gap_key)
);
CREATE TABLE IF NOT EXISTS runtime_rating_evidence (
    review_id TEXT PRIMARY KEY REFERENCES jobs(review_id),
    captured_at TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    projection_json TEXT NOT NULL
);
"""

MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS task_records (
    task_id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE NOT NULL,
    task_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL REFERENCES task_records(task_id),
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, event_id)
);
CREATE TABLE IF NOT EXISTS task_reviews (
    task_id TEXT PRIMARY KEY REFERENCES task_records(task_id),
    review_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_share_previews (
    preview_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_records(task_id),
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_setup (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    mode TEXT NOT NULL,
    runtime TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_records_status ON task_records(json_extract(task_json, '$.status'));
"""

MIGRATIONS = [(1, MIGRATION_1), (2, MIGRATION_2), (3, MIGRATION_3),
              (4, MIGRATION_4)]

CONTEXT_SUBSET = ("review_id", "job_type", "task", "action", "result",
                  "verification", "context", "resources")


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    pass


class StaleRevisionError(StoreError):
    pass


class ImportAbort(StoreError):
    pass


def resolve_data_dir(explicit=None):
    override = explicit or os.environ.get("APR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "Agent Performance Review"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "agent-performance-review"
    return Path.home() / ".local" / "share" / "agent-performance-review"


def sys_platform():
    import sys
    return sys.platform


def ensure_private_dir(path):
    """Create data dir with 0700; refuse to operate through a symlink."""
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked data directory: {path}")
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise StoreError(f"cannot secure data directory: {exc}") from exc
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked data directory: {path}")


def secure_file(path):
    """Refuse symlinks; ensure regular file is 0600.

    chmod failures surface as StoreError: silently keeping a group- or
    world-readable credential, config, or database file would defeat the
    private-file contract.
    """
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked sensitive file: {path}")
    if os.path.exists(path) and not os.path.isfile(path):
        raise StoreError(f"sensitive path is not a regular file: {path}")
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise StoreError(
                f"cannot secure sensitive file {path}: {exc}") from exc


def secure_existing_private(path, label="sensitive file"):
    """Enforce 0600 on an existing private file (locks, logs); refuse links."""
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked {label}: {path}")
    if not os.path.isfile(path):
        raise StoreError(f"{label} is not a regular file: {path}")
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise StoreError(f"cannot secure {label} {path}: {exc}") from exc


#: Per-data-directory advisory lock serializing store bootstrap (pre-open
#: sidefile checks, SQLite connect/mode setup, migrations, file securing)
#: and close (checkpoint, close, sidefile securing) across processes and
#: threads. Held only at those boundaries, never for normal operations.
#: Owner-only regular file; symlinks are refused, never followed.
INIT_LOCK_NAME = "init.lock"


def _init_lock_path(data_dir):
    return os.path.join(str(data_dir), INIT_LOCK_NAME)


def _acquire_init_lock(data_dir, timeout=None):
    """Create the owner-only init lock and take an exclusive flock.

    Returns the open fd; the caller must pass it to _release_init_lock.
    Raises StoreError on symlinks, non-regular files, or lock failures.
    """
    path = _init_lock_path(data_dir)
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked init lock: {path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | nofollow, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP or os.path.islink(path):
            raise StoreError(
                f"refusing symlinked init lock: {path}") from exc
        raise StoreError(f"cannot open init lock {path}: {exc}") from exc
    try:
        try:
            mode = os.fstat(fd).st_mode
        except OSError as exc:
            raise StoreError(
                f"cannot inspect init lock {path}: {exc}") from exc
        if not stat.S_ISREG(mode):
            raise StoreError(f"init lock is not a regular file: {path}")
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise StoreError(
                f"cannot secure init lock {path}: {exc}") from exc
        if timeout is None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise StoreError(
                    f"cannot serialize store initialization: {exc}") from exc
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StoreError("store initialization lock timed out")
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                except OSError as exc:
                    raise StoreError(
                        f"cannot serialize store initialization: {exc}") from exc
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _release_init_lock(fd):
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _chmod_private_nofollow(path):
    """Set mode 0600 by path without opening the file.

    Uses follow_symlinks=False where the platform supports it, so an exact
    path that is (or has become) a symlink is never followed. Plain by-path
    chmod never opens a file descriptor, so SQLite's POSIX process locks on
    the database and sidefiles are left intact.
    """
    if os.chmod in getattr(os, "supports_follow_symlinks", set()):
        os.chmod(path, 0o600, follow_symlinks=False)
    else:
        os.chmod(path, 0o600)


def _secure_db_candidate(candidate):
    """Enforce 0600 on one existing database sidecar without following links.

    Never opens the candidate: opening and closing an extra file descriptor
    on reviews.db/-shm releases this process's POSIX locks held by SQLite
    (SQLite howtocorrupt.html section 2.2), silently dropping the database
    lock even while another connection in this process still uses it. Only
    lstat/islink validation and by-path chmod are used here; the init.lock
    descriptor is a separate file and is unaffected.

    A candidate that disappears between the existence check and securing
    (SQLite checkpoint/commit cleanup racing a concurrent bootstrap) is a
    legitimate absence, not an error. Permission failures still raise
    StoreError, and symlinks/non-regular files are always refused.
    """
    try:
        mode = os.lstat(candidate).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StoreError(
            f"cannot secure database file {candidate}: {exc}") from exc
    if stat.S_ISLNK(mode) or os.path.islink(candidate):
        raise StoreError(
            f"refusing symlinked database file: {candidate}")
    if not stat.S_ISREG(mode):
        raise StoreError(
            f"database path is not a regular file: {candidate}")
    try:
        _chmod_private_nofollow(candidate)
    except FileNotFoundError:
        return
    except OSError as exc:
        if os.path.islink(candidate):
            raise StoreError(
                f"refusing symlinked database file: {candidate}") from exc
        raise StoreError(
            f"cannot secure database file {candidate}: {exc}") from exc
    # A swap between validation and chmod must not pass silently: a symlink
    # (or non-regular file) observed after securing is still refused.
    try:
        post = os.lstat(candidate).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StoreError(
            f"cannot secure database file {candidate}: {exc}") from exc
    if stat.S_ISLNK(post) or os.path.islink(candidate):
        raise StoreError(
            f"refusing symlinked database file: {candidate}")
    if not stat.S_ISREG(post):
        raise StoreError(
            f"database path is not a regular file: {candidate}")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    #: SQLite sidecar files SQLite may create beside the database.
    DB_SUFFIXES = ("", "-wal", "-shm", "-journal")

    def __init__(self, data_dir, busy_timeout_ms=10000, init_lock_timeout=None):
        self.data_dir = Path(data_dir)
        self.db_path = str(self.data_dir / "reviews.db")
        self._lock = threading.RLock()
        self._conn = None
        if (isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int)
                or busy_timeout_ms < 1 or busy_timeout_ms > 10000):
            raise StoreError("invalid SQLite busy timeout")
        self.busy_timeout_ms = busy_timeout_ms
        self.init_lock_timeout = init_lock_timeout
        ensure_private_dir(self.data_dir)
        # Serialize the whole bootstrap across processes and threads: the
        # pre-open sidefile checks, SQLite connect/mode setup, migrations,
        # and file securing must not interleave with another cold open or
        # a concurrent close. The lock is held only here (and briefly in
        # close), never for normal long-lived operations. Lock order is
        # always init-lock outer, per-instance lock inner.
        lock_fd = _acquire_init_lock(self.data_dir, init_lock_timeout)
        try:
            if os.path.islink(self.data_dir):
                raise StoreError(
                    f"refusing symlinked data directory: {self.data_dir}")
            # Fail closed BEFORE any SQLite open: connecting first would let
            # SQLite read or create files through a symlinked target, and
            # would apply PRAGMAs/migrations before the file check runs.
            self._check_db_files_preopen()
            new_db = not os.path.exists(self.db_path)
            try:
                self._conn = sqlite3.connect(
                    self.db_path, check_same_thread=False,
                    isolation_level=None, timeout=busy_timeout_ms / 1000)
            except sqlite3.Error as exc:
                raise StoreError(
                    f"cannot open database {self.db_path}: {exc}") from exc
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error as exc:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                raise StoreError(
                    f"cannot initialize database {self.db_path}: {exc}"
                ) from exc
            self._conn.row_factory = sqlite3.Row
            if new_db:
                try:
                    _chmod_private_nofollow(self.db_path)
                except OSError as exc:
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        pass
                    self._conn = None
                    raise StoreError(
                        f"cannot secure new database {self.db_path}: {exc}"
                    ) from exc
                if os.path.islink(self.db_path):
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        pass
                    self._conn = None
                    raise StoreError(
                        f"refusing symlinked database file: {self.db_path}")
            try:
                self.apply_migrations()
                from task_store import TaskStore
                self.tasks = TaskStore(self)
            except Exception:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                raise
            self._secure_db_files()
        finally:
            _release_init_lock(lock_fd)

    def _check_db_files_preopen(self):
        """Validate every existing database sidecar before SQLite opens any.

        Refuses symlinks and non-regular files, and enforces 0600 on what
        already exists, so no SQLite call ever touches an unsafe target.
        Only exact-path `islink` checks are used: a data directory that
        merely lives under an aliased parent (e.g. macOS /tmp) is unaffected.
        A sidecar that vanishes between the check and securing (SQLite
        cleanup racing a concurrent bootstrap) is a legitimate absence.
        """
        for suffix in self.DB_SUFFIXES:
            _secure_db_candidate(self.db_path + suffix)

    @classmethod
    def open(cls, data_dir=None, busy_timeout_ms=10000, init_lock_timeout=None):
        return cls(resolve_data_dir(data_dir), busy_timeout_ms, init_lock_timeout)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _secure_db_files(self):
        for suffix in self.DB_SUFFIXES:
            _secure_db_candidate(self.db_path + suffix)

    def close(self):
        # Coordinate with concurrent bootstraps: checkpoint, connection
        # close, and sidefile securing run under the same per-directory
        # init lock taken outer, per-instance lock inner (the same order
        # as __init__), so a racing cold open cannot observe a
        # half-checkpointed store. Normal operations never take this lock.
        # If the directory or lock is gone, still close the connection
        # rather than leaking it behind a lock error.
        try:
            lock_fd = _acquire_init_lock(self.data_dir, self.init_lock_timeout)
        except StoreError:
            if self.init_lock_timeout is not None:
                with self._lock:
                    if self._conn is not None:
                        try: self._conn.close()
                        finally: self._conn = None
                raise
            lock_fd = None
        try:
            with self._lock:
                if self._conn is None:
                    if lock_fd is not None:
                        self._secure_db_files()
                    return
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                try:
                    self._conn.close()
                finally:
                    self._conn = None
                if lock_fd is not None:
                    self._secure_db_files()
        finally:
            if lock_fd is not None:
                _release_init_lock(lock_fd)

    # -- migrations -----------------------------------------------------
    def migration_versions(self):
        table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'schema_migrations'").fetchone()
        if table is None:
            return []
        rows = self._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()
        return [row[0] for row in rows]

    def apply_migrations(self):
        # Version read, DDL, and version registration run inside one
        # BEGIN IMMEDIATE write transaction using individual statements:
        # executescript() would implicitly commit each statement under
        # isolation_level=None, breaking atomicity. The write lock serializes
        # concurrent first opens: the loser blocks at BEGIN, then reads the
        # winner's committed versions and converges instead of inserting
        # duplicate rows.
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StoreError(
                    f"cannot lock database for migrations: {exc}") from exc
            try:
                existing = set(self.migration_versions())
                for version, sql in MIGRATIONS:
                    if version in existing:
                        continue
                    for statement in (
                            part.strip() for part in sql.split(";")):
                        if statement:
                            self._conn.execute(statement)
                    try:
                        self._conn.execute(
                            "INSERT INTO schema_migrations"
                            " (version, applied_at) VALUES (?, ?)",
                            (version, now_iso()))
                    except sqlite3.IntegrityError as exc:
                        # A concurrent first open committed first; converge
                        # on the winner's schema instead of crashing.
                        existing = set(self.migration_versions())
                        if version in existing:
                            continue
                        raise StoreError(
                            f"migration {version} conflicts with concurrent"
                            f" schema change: {exc}") from exc
                    existing.add(version)
                self._conn.execute("COMMIT")
            except StoreError:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(
                    f"migration failed; rolled back: {exc}") from exc
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # -- jobs -----------------------------------------------------------
    @staticmethod
    def _snapshot_of(review):
        return {k: review[k] for k in CONTEXT_SUBSET if k in review}

    def request_job(self, snapshot):
        if not isinstance(snapshot, dict) or not snapshot.get("review_id"):
            raise StoreError("snapshot must include review_id")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT job_token, revision, status FROM jobs"
                    " WHERE review_id = ?",
                    (snapshot["review_id"],)).fetchone()
                if row is None:
                    token = secrets.token_urlsafe(24)
                    self._conn.execute(
                        "INSERT INTO jobs (review_id, job_token, snapshot, revision,"
                        " status, updated_at) VALUES (?, ?, ?, 1, 'pending', ?)",
                        (snapshot["review_id"], token,
                         json.dumps(snapshot, ensure_ascii=False), now_iso()))
                    self._conn.execute("COMMIT")
                    return token, 1, True
                if row["status"] == "saved":
                    # Saved reviews are frozen: later requests return the
                    # saved URL/state without touching the rated snapshot.
                    self._conn.execute("ROLLBACK")
                    return row["job_token"], int(row["revision"]), False
                new_rev = int(row["revision"]) + 1
                self._conn.execute(
                    "UPDATE jobs SET snapshot = ?, revision = ?, updated_at = ?"
                    " WHERE review_id = ?",
                    (json.dumps(snapshot, ensure_ascii=False), new_rev, now_iso(),
                     snapshot["review_id"]))
                self._conn.execute("COMMIT")
                return row["job_token"], new_rev, False
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _card_from_row(self, row):
        if row is None:
            return None
        card = {"review_id": row["review_id"],
                "job_token": row["job_token"],
                "snapshot": json.loads(row["snapshot"]),
                "revision": int(row["revision"]),
                "status": row["status"],
                "saved": None}
        if row["status"] == "saved" and row["saved_full"]:
            saved = json.loads(row["saved_full"])
            card["saved"] = {"snapshot": json.loads(row["saved_snapshot"])
                             if row["saved_snapshot"] else None,
                             "ratings": saved.get("ratings"),
                             "completion": saved.get("completion"),
                             "recorded_at": row["saved_at"],
                             "source_digest": saved.get("source_digest")}
        return card

    def get_card(self, job_token):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_token = ?", (job_token,)).fetchone()
        return self._card_from_row(row)

    def get_card_by_review_id(self, review_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE review_id = ?", (review_id,)).fetchone()
        return self._card_from_row(row)

    # -- native runtime capture ---------------------------------------
    def _runtime_salt(self):
        row = self._conn.execute(
            "SELECT value FROM runtime_metadata WHERE key = 'hmac_salt'").fetchone()
        if row:
            return bytes(row[0])
        salt = secrets.token_bytes(32)
        self._conn.execute(
            "INSERT INTO runtime_metadata (key, value) VALUES ('hmac_salt', ?)",
            (salt,))
        return salt

    @staticmethod
    def _opaque(salt, domain, value):
        return hmac.new(salt, (domain + "\0" + value).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def ensure_runtime_job(self, session_id, source="startup", model_slug=None,
                           adapter_version="1"):
        """Bind one native session to a sparse local review; raw ID is never stored."""
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise StoreError("native session identity is required")
        if source not in {"startup", "resume", "compact", "clear", "fork"}:
            raise StoreError("unsupported session source")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                salt = self._runtime_salt()
                native_key = self._opaque(salt, "codex-session", session_id)
                row = self._conn.execute(
                    "SELECT review_id, start_source FROM runtime_bindings"
                    " WHERE runtime='codex' AND installation_key='local' AND native_chat_key=?",
                    (native_key,)).fetchone()
                if row:
                    if source in {"clear", "fork"}:
                        raise ConflictError("clear/fork continuity with a reused native ID is unsupported")
                    blocked = self._conn.execute(
                        "SELECT 1 FROM runtime_gaps WHERE review_id=?"
                        " AND gap_key='identity_continuity_unsupported'", (row["review_id"],)).fetchone()
                    if blocked:
                        raise ConflictError("native identity continuity is unsupported after clear/fork")
                    self._conn.execute(
                        "UPDATE runtime_bindings SET last_seen_at=? WHERE runtime='codex'"
                        " AND installation_key='local' AND native_chat_key=?",
                        (now_iso(), native_key))
                    self._conn.execute("COMMIT")
                    return row["review_id"]
                review_id = "apr-" + secrets.token_hex(16)
                token = secrets.token_urlsafe(24)
                now = now_iso()
                snapshot = {"review_id": review_id, "job_type": "other",
                            "task": "No task summary captured",
                            "action": "Native lifecycle metadata captured automatically",
                            "result": "unknown", "verification": "unknown",
                            "context": {"runtime": "codex", "agent": "unknown",
                                        "model": model_slug or "unknown",
                                        "connections": "unknown", "loops": "unknown",
                                        "roles": "unknown"},
                            "resources": {"elapsed_time": "unknown", "tokens": "unknown",
                                           "cost": "unknown", "tool_calls": "unknown"}}
                self._conn.execute(
                    "INSERT INTO jobs (review_id, job_token, snapshot, revision, status, updated_at)"
                    " VALUES (?, ?, ?, 1, 'pending', ?)",
                    (review_id, token, json.dumps(snapshot), now))
                self._conn.execute(
                    "INSERT INTO runtime_bindings (runtime, installation_key, native_chat_key,"
                    " identity_source, review_id, first_seen_at, last_seen_at, start_source,"
                    " adapter_version) VALUES ('codex','local',?,'native-session-id',?,?,?, ?,?)",
                    (native_key, review_id, now, now, source, adapter_version))
                self._conn.execute("COMMIT")
                return review_id
            except Exception:
                try: self._conn.execute("ROLLBACK")
                except sqlite3.Error: pass
                raise

    def ingest_runtime_event(self, session_id, event):
        """Persist a validated, content-free observation idempotently."""
        from review_schema import validate_runtime_event
        event = validate_runtime_event(event)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                salt = self._runtime_salt()
                native_key = self._opaque(salt, "codex-session", session_id)
                binding = self._conn.execute(
                    "SELECT review_id FROM runtime_bindings WHERE runtime='codex'"
                    " AND installation_key='local' AND native_chat_key=?", (native_key,)).fetchone()
                if not binding:
                    raise StoreError("session has no captured binding; start capture first")
                review_id = binding["review_id"]
                blocked = self._conn.execute(
                    "SELECT 1 FROM runtime_gaps WHERE review_id=?"
                    " AND gap_key='identity_continuity_unsupported'", (review_id,)).fetchone()
                if blocked:
                    raise ConflictError("native identity continuity is unsupported after clear/fork")
                turn_key = self._opaque(salt, "codex-turn", event["turn_id"]) if event.get("turn_id") else None
                tool_key = self._opaque(salt, "codex-tool", event["tool_use_id"]) if event.get("tool_use_id") else None
                original_kind = event["kind"]
                key_material = json.dumps([native_key, original_kind, event.get("turn_id"),
                                           event.get("tool_use_id"), event.get("source")],
                                          separators=(",", ":"), ensure_ascii=True)
                event_key = self._opaque(salt, "codex-event", key_material)
                if event["kind"] in {"tool_pre", "tool_post"}:
                    root = (self._conn.execute(
                        "SELECT 1 FROM runtime_turns WHERE review_id=? AND turn_key=?",
                        (review_id, turn_key)).fetchone() if turn_key else None)
                    if not root:
                        event["kind"] = "unattributed_tool_" + event["kind"].split("_")[1]
                        tool_key = None
                now = event["observed_at_utc"]
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO runtime_events (event_key, review_id, turn_key, kind,"
                    " observed_at_utc, observed_monotonic_ns, clock_epoch, model_slug,"
                    " safe_tool_category, tool_key, source, adapter_version, schema_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (event_key, review_id, turn_key, event["kind"], now,
                     event["observed_monotonic_ns"], event.get("clock_epoch"),
                     event.get("model_slug"), event.get("safe_tool_category"), tool_key,
                     event.get("source"), event["adapter_version"]))
                if cur.rowcount:
                    if event["kind"] == "turn_start" and turn_key:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO runtime_turns (review_id, turn_key,"
                            " first_observed_at, first_model, last_model, coverage)"
                            " VALUES (?,?,?,?,?,'partial')",
                            (review_id, turn_key, now, event.get("model_slug"), event.get("model_slug")))
                    elif event["kind"] in {"turn_stopped", "turn_interrupted"} and turn_key:
                        self._conn.execute(
                            "UPDATE runtime_turns SET terminal_observed_at=COALESCE(terminal_observed_at,?),"
                            " terminal_kind=COALESCE(terminal_kind,?), coverage='observed_terminal'"
                            " WHERE review_id=? AND turn_key=?",
                            (now, event["kind"], review_id, turn_key))
                    elif event.get("model_slug") and turn_key:
                        self._conn.execute(
                            "UPDATE runtime_turns SET last_model=? WHERE review_id=? AND turn_key=?",
                            (event["model_slug"], review_id, turn_key))
                    self._conn.execute(
                        "UPDATE jobs SET revision=revision+1, updated_at=?"
                        " WHERE review_id=? AND status='pending'", (now, review_id))
                self._conn.execute(
                    "UPDATE runtime_bindings SET last_seen_at=? WHERE runtime='codex'"
                    " AND installation_key='local' AND native_chat_key=?", (now, native_key))
                self._conn.execute("COMMIT")
                return {"review_id": review_id, "inserted": bool(cur.rowcount)}
            except Exception:
                try: self._conn.execute("ROLLBACK")
                except sqlite3.Error: pass
                raise

    def runtime_review_projection(self, review_id):
        with self._lock:
            own_read_transaction = not self._conn.in_transaction
            if own_read_transaction:
                self._conn.execute("BEGIN")
            try:
                binding = self._conn.execute(
                    "SELECT * FROM runtime_bindings WHERE review_id=?", (review_id,)).fetchone()
                events = self._conn.execute(
                    "SELECT * FROM runtime_events WHERE review_id=? ORDER BY observed_at_utc, event_key",
                    (review_id,)).fetchall()
                turns = self._conn.execute(
                    "SELECT * FROM runtime_turns WHERE review_id=? ORDER BY first_observed_at",
                    (review_id,)).fetchall()
                tool_count = self._conn.execute(
                    "SELECT COUNT(DISTINCT tool_key) FROM runtime_events WHERE review_id=?"
                    " AND kind='tool_pre' AND tool_key IS NOT NULL", (review_id,)).fetchone()[0]
                category_rows = self._conn.execute(
                    "SELECT safe_tool_category,COUNT(DISTINCT tool_key) AS count"
                    " FROM runtime_events WHERE review_id=? AND kind='tool_pre'"
                    " AND tool_key IS NOT NULL GROUP BY safe_tool_category", (review_id,)).fetchall()
                saved = self._conn.execute(
                    "SELECT saved_at FROM jobs WHERE review_id=?", (review_id,)).fetchone()[0]
                event_count = self._conn.execute(
                    "SELECT COUNT(*) FROM runtime_events WHERE review_id=?", (review_id,)).fetchone()[0]
                rated_count_row = self._conn.execute(
                    "SELECT event_count,projection_json FROM runtime_rating_evidence WHERE review_id=?",
                    (review_id,)).fetchone()
                rated_event_count = rated_count_row[0] if rated_count_row else None
                stored_gaps = self._conn.execute(
                    "SELECT gap_key,count,last_seen_at FROM runtime_gaps WHERE review_id=?",
                    (review_id,)).fetchall()
                if own_read_transaction:
                    self._conn.execute("COMMIT")
            except Exception:
                if own_read_transaction:
                    try: self._conn.execute("ROLLBACK")
                    except sqlite3.Error: pass
                raise
        if not binding:
            return None
        clock_anomaly = False
        observed = [e["observed_at_utc"] for e in events]
        observed_instants = []
        for value in observed:
            try:
                observed_instants.append((datetime.fromisoformat(value), value))
            except ValueError:
                clock_anomaly = True
        first_observed = min(observed_instants)[1] if observed_instants else None
        last_observed = max(observed_instants)[1] if observed_instants else None
        durations = []
        projected_turns = []
        for turn in turns:
            duration = None
            if turn["first_observed_at"] and turn["terminal_observed_at"]:
                try:
                    delta = (datetime.fromisoformat(turn["terminal_observed_at"]) -
                             datetime.fromisoformat(turn["first_observed_at"])).total_seconds()
                    if delta >= 0:
                        duration = delta
                        durations.append(delta)
                    else:
                        clock_anomaly = True
                except ValueError:
                    pass
            projected_turns.append({"elapsed_seconds": duration,
                                    "terminal_kind": turn["terminal_kind"],
                                    "coverage": turn["coverage"]})
        gaps = []
        if binding["start_source"] != "startup": gaps.append("late_start")
        if tool_count: gaps.append("subagent_attribution_unverified")
        if any(t["coverage"] != "observed_terminal" for t in turns): gaps.append("incomplete_turn")
        if any(e["kind"].startswith("unattributed_tool") for e in events): gaps.append("unattributed_tool_events")
        pre_ids = {e["tool_key"] for e in events if e["kind"] == "tool_pre" and e["tool_key"]}
        post_ids = {e["tool_key"] for e in events if e["kind"] == "tool_post" and e["tool_key"]}
        if pre_ids - post_ids: gaps.append("missing_tool_post")
        if post_ids - pre_ids: gaps.append("missing_tool_pre")
        tool_spans = []
        tool_events = {}
        for event in events:
            if event["tool_key"]:
                tool_events.setdefault(event["tool_key"], {})[event["kind"]] = event["observed_at_utc"]
        for phases in tool_events.values():
            if "tool_pre" not in phases or "tool_post" not in phases:
                continue
            try:
                span = (datetime.fromisoformat(phases["tool_post"]) -
                        datetime.fromisoformat(phases["tool_pre"])).total_seconds()
                if span >= 0:
                    tool_spans.append(span)
                else:
                    clock_anomaly = True
            except ValueError:
                clock_anomaly = True
        turn_keys = {t["turn_key"] for t in turns}
        turn_map = {t["turn_key"]: t for t in turns}
        if any(e["kind"] in {"turn_stopped", "turn_interrupted"} and
               (e["turn_key"] not in turn_map or
                not turn_map[e["turn_key"]]["terminal_observed_at"])
               for e in events):
            gaps.append("terminal_without_observed_start")
        gap_watermark = {row["gap_key"]: {"count": row["count"],
                                           "last_seen_at": row["last_seen_at"]}
                         for row in stored_gaps}
        gaps.extend(gap_watermark)
        try:
            chat_wall = ((max(observed_instants)[0] - min(observed_instants)[0]).total_seconds()
                         if len(observed_instants) > 1 else 0)
            if chat_wall < 0:
                chat_wall = None
                clock_anomaly = True
        except ValueError:
            chat_wall = None
        if clock_anomaly and "clock_anomaly" not in gaps: gaps.append("clock_anomaly")
        after_rating_count = (max(0, event_count - rated_event_count)
                              if saved is not None and rated_event_count is not None else 0)
        has_rating_watermark = saved is not None and rated_count_row is not None
        rated_projection = (json.loads(rated_count_row["projection_json"])
                            if has_rating_watermark else {})
        rated_gaps = rated_projection.get("gap_watermark", {})
        post_rating_gap_keys = (sorted(
            key for key, value in gap_watermark.items()
            if (key not in rated_gaps or value["count"] != rated_gaps[key].get("count")
                or value["last_seen_at"] != rated_gaps[key].get("last_seen_at")))
            if has_rating_watermark else [])
        after_rating_gap_count = sum(
            max(0, value["count"] - rated_gaps.get(key, {}).get("count", 0))
            for key, value in gap_watermark.items() if key in post_rating_gap_keys)
        after_rating = (last_observed if after_rating_count else None)
        if post_rating_gap_keys:
            gap_times = [gap_watermark[key]["last_seen_at"] for key in post_rating_gap_keys]
            after_rating = max([v for v in [after_rating, *gap_times] if v])
        return {"runtime": "codex", "capture_started_at": binding["first_seen_at"],
                "first_observed_at": first_observed,
                "last_observed_at": last_observed,
                "start_source": binding["start_source"], "adapter_version": binding["adapter_version"],
                "observed_turn_count": len(turns), "open_turn_count": sum(
                    1 for t in turns if not t["terminal_observed_at"]),
                "event_count": event_count,
                "post_rating_event_count": after_rating_count,
                "post_rating_gap_count": after_rating_gap_count,
                "post_rating_gap_keys": post_rating_gap_keys,
                "gap_watermark": gap_watermark,
                "tool_call_count": tool_count,
                "tool_categories": {row["safe_tool_category"]: row["count"]
                                    for row in category_rows if row["safe_tool_category"]},
                "turn_elapsed_seconds": durations,
                "tool_elapsed_seconds": tool_spans,
                "turns": projected_turns,
                "observed_turn_elapsed_seconds_total": sum(durations),
                "chat_wall_seconds": chat_wall,
                "elapsed_basis": "UTC hook receipt wall time; monotonic epoch unavailable",
                "models": sorted({e["model_slug"] for e in events
                                  if e["model_slug"] and e["kind"] == "turn_start"}),
                "tokens": "unknown", "cost": "unknown",
                "coverage": "partial", "coverage_gaps": sorted(set(gaps)),
                "evidence_after_rating": after_rating}

    def mark_runtime_gap(self, session_id, gap_key="capture_error"):
        """Record only a bounded failure class against a known native binding."""
        if gap_key not in {"capture_error", "identity_continuity_unsupported",
                           "missing_tool_identity", "missing_turn_identity",
                           "lifecycle_occurrence_unverifiable"}:
            gap_key = "capture_error"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                salt = self._runtime_salt()
                native_key = self._opaque(salt, "codex-session", session_id)
                row = self._conn.execute(
                    "SELECT review_id FROM runtime_bindings WHERE runtime='codex'"
                    " AND installation_key='local' AND native_chat_key=?", (native_key,)).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE jobs SET revision=revision+1, updated_at=?"
                        " WHERE review_id=? AND status='pending'",
                        (now_iso(), row[0]))
                    self._conn.execute(
                        "INSERT INTO runtime_gaps (review_id,gap_key,count,last_seen_at)"
                        " VALUES (?,?,1,?) ON CONFLICT(review_id,gap_key) DO UPDATE SET"
                        " count=count+1,last_seen_at=excluded.last_seen_at",
                        (row[0], gap_key, now_iso()))
                self._conn.execute("COMMIT")
                return bool(row)
            except Exception:
                try: self._conn.execute("ROLLBACK")
                except sqlite3.Error: pass
                raise

    def runtime_rating_projection(self, review_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT captured_at,event_count,projection_json"
                " FROM runtime_rating_evidence WHERE review_id=?", (review_id,)).fetchone()
        if not row:
            return None
        return {"captured_at": row["captured_at"], "event_count": row["event_count"],
                "projection": json.loads(row["projection_json"])}

    def list_runtime_reviews(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.review_id, b.first_seen_at, b.last_seen_at, b.start_source, j.job_token"
                " FROM runtime_bindings b JOIN jobs j USING (review_id)"
                " ORDER BY b.last_seen_at DESC").fetchall()
        return [dict(r) for r in rows]

    # -- ratings --------------------------------------------------------
    def save_rating(self, full, expected_revision=None):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE review_id = ?",
                    (full["review_id"],)).fetchone()
                if row is None:
                    token = secrets.token_urlsafe(24)
                    snapshot = self._snapshot_of(full)
                    self._conn.execute(
                        "INSERT INTO jobs (review_id, job_token, snapshot, revision,"
                        " status, updated_at) VALUES (?, ?, ?, 1, 'pending', ?)",
                        (full["review_id"], token,
                         json.dumps(snapshot, ensure_ascii=False), now_iso()))
                    row = self._conn.execute(
                        "SELECT * FROM jobs WHERE review_id = ?",
                        (full["review_id"],)).fetchone()
                if (expected_revision is not None
                        and int(row["revision"]) != int(expected_revision)
                        and row["status"] == "pending"):
                    raise StaleRevisionError(
                        "This card changed since it was opened. Reload the link to"
                        " review the latest snapshot, then save again.")
                if row["status"] == "saved" and row["saved_full"]:
                    stored = json.loads(row["saved_full"])
                    if self._same_full(stored, full):
                        self._conn.execute("ROLLBACK")
                        return {"status": "already_recorded",
                                "review_id": full["review_id"],
                                "recorded_at": row["saved_at"],
                                "scope": SCOPE}
                    raise ConflictError(
                        f"review_id {full['review_id']} is already saved with"
                        " different content; use a new review_id for a new review")
                recorded_at = full.get("recorded_at") or now_iso()
                saved_full = dict(full)
                saved_full.pop("recorded_at", None)
                projection = self.runtime_review_projection(full["review_id"])
                if projection is not None:
                    event_count = self._conn.execute(
                        "SELECT COUNT(*) FROM runtime_events WHERE review_id=?",
                        (full["review_id"],)).fetchone()[0]
                    self._conn.execute(
                        "INSERT OR REPLACE INTO runtime_rating_evidence"
                        " (review_id,captured_at,event_count,projection_json) VALUES (?,?,?,?)",
                        (full["review_id"], recorded_at, event_count,
                         json.dumps(projection, ensure_ascii=False)))
                self._conn.execute(
                    "UPDATE jobs SET status = 'saved', saved_snapshot = ?,"
                    " saved_full = ?, saved_at = ?, updated_at = ?"
                    " WHERE review_id = ?",
                    (json.dumps(self._snapshot_of(full), ensure_ascii=False),
                     json.dumps(saved_full, ensure_ascii=False),
                     recorded_at, now_iso(), full["review_id"]))
                self._conn.execute("COMMIT")
                return {"status": "recorded", "review_id": full["review_id"],
                        "recorded_at": recorded_at, "scope": SCOPE}
            except (StaleRevisionError, ConflictError):
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    @staticmethod
    def _same_full(stored, full):
        keys = ("review_id", "job_type", "task", "action", "result", "verification",
                "completion", "gap_reasons", "context", "resources", "ratings")
        for key in keys:
            if stored.get(key) != full.get(key):
                return False
        return True

    def saved_count(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'saved'").fetchone()
        return int(row[0])

    def summary(self, job_type=None):
        # List membership (==, never hashing) is safe for unhashable filters
        # such as lists and dicts: every non-None value outside JOB_TYPES is
        # a validation error, never a silent zero-count and never a crash.
        if job_type is not None and (
                not isinstance(job_type, str) or job_type not in JOB_TYPES):
            raise ValidationError(
                f"job_type must be one of: {', '.join(JOB_TYPES)}")
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot, saved_full FROM jobs WHERE status = 'saved'").fetchall()
            task_rows = self._conn.execute(
                "SELECT t.task_json,r.review_json FROM task_reviews r"
                " JOIN task_records t USING(task_id)"
                " WHERE t.task_id NOT LIKE 'legacy:%'").fetchall()
        groups = {}
        for row in rows:
            saved = json.loads(row["saved_full"])
            kind = saved.get("job_type")
            ratings = saved.get("ratings")
            if not isinstance(ratings, dict):
                continue
            if job_type and kind != job_type:
                continue
            groups.setdefault(kind, []).append(saved)
        for row in task_rows:
            task = json.loads(row["task_json"])
            review = json.loads(row["review_json"])
            kind = task.get("category")
            if job_type and kind != job_type:
                continue
            groups.setdefault(kind, []).append(review | {"job_type": kind})

        def summarize(items):
            score_rows = [item["ratings"] for item in items]
            rating_keys = ("overall", "quality", "speed", "resource_efficiency")
            counts = {key: sum(key in ratings for ratings in score_rows)
                      for key in rating_keys}
            means = {key: (round(sum(r[key] for r in score_rows if key in r)
                                / counts[key], 2) if counts[key] else None)
                     for key in rating_keys} if score_rows else None
            outcomes = [i.get("completion") for i in items
                        if i.get("completion") in ("yes", "no")]
            rate = round(outcomes.count("yes") / len(outcomes), 2) if outcomes else None
            return {"count": len(items), "completion_rate": rate,
                    "mean_ratings": means,
                    "rating_counts": counts if score_rows else {key: 0 for key in rating_keys}}

        every = [item for items in groups.values() for item in items]
        return {"scope": SCOPE, "job_type_filter": job_type or "all",
                **summarize(every),
                "by_job_type": {k: summarize(v) for k, v in groups.items()}}

    # -- legacy JSONL import --------------------------------------------
    #: Envelope fields present in real legacy records but not part of the
    #: review schema. `scope: "local"` marks the old local ledger format and
    #: is accepted; any other scope is rejected.
    LEGACY_SCOPE = "local"

    def import_jsonl(self, path, dry_run=False):
        with open(path, "rb") as handle:
            raw = handle.read()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportAbort(f"legacy file is not UTF-8: {exc}") from exc
        entries = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ImportAbort(f"line {number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ImportAbort(f"line {number}: must be an object")
            scope = record.get("scope", self.LEGACY_SCOPE)
            if scope != self.LEGACY_SCOPE:
                raise ImportAbort(
                    f"line {number}: unsupported scope {scope!r};"
                    f" only {self.LEGACY_SCOPE!r} legacy records can be imported")
            try:
                full = validate_review(
                    {k: v for k, v in record.items()
                     if k not in ("recorded_at", "scope")})
            except ValidationError as exc:
                raise ImportAbort(f"line {number}: {exc}") from exc
            recorded_at = record.get("recorded_at")
            if recorded_at is not None and not isinstance(recorded_at, str):
                raise ImportAbort(f"line {number}: recorded_at must be a string")
            entries.append((full, recorded_at or now_iso()))
        seen = {}
        for full, _ in entries:
            key = full["review_id"]
            blob = json.dumps(full, sort_keys=True)
            if key in seen and seen[key] != blob:
                raise ImportAbort(
                    f"legacy file has conflicting entries for review_id {key}")
            seen[key] = blob
        # Read, plan, and apply run inside one BEGIN IMMEDIATE write
        # transaction: a concurrent writer (service or another store) blocks
        # until the import commits or rolls back, so the plan it validated is
        # the state it writes against. A conflicting concurrent write that
        # still collides surfaces as ImportAbort, never a raw IntegrityError.
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StoreError(
                    f"cannot lock database for import: {exc}") from exc
            try:
                existing = {row["review_id"]: row for row in self._conn.execute(
                    "SELECT review_id, status, saved_full, saved_at FROM jobs"
                ).fetchall()}
                imported, skipped = 0, 0
                planned = []
                planned_ids = set()
                for full, recorded_at in entries:
                    if full["review_id"] in planned_ids:
                        skipped += 1  # identical duplicate row in this file
                        continue
                    row = existing.get(full["review_id"])
                    candidate = dict(full)
                    if row is None:
                        planned.append((full, recorded_at))
                        planned_ids.add(full["review_id"])
                        continue
                    if row["status"] != "saved":
                        raise ImportAbort(
                            f"review_id {full['review_id']} already exists as a"
                            " pending job; import aborted without partial writes")
                    stored = json.loads(row["saved_full"])
                    if self._same_full(stored, candidate):
                        skipped += 1
                        continue
                    raise ImportAbort(
                        f"review_id {full['review_id']} already saved with different"
                        " content; import aborted without partial writes")
                if dry_run:
                    self._conn.execute("ROLLBACK")
                    return {"imported": len(planned),
                            "skipped_identical": skipped,
                            "source_digest": digest, "dry_run": True}
                for full, recorded_at in planned:
                    token = secrets.token_urlsafe(24)
                    saved_full = dict(full, source_digest=digest,
                                      provenance="jsonl-import")
                    self._conn.execute(
                        "INSERT INTO jobs (review_id, job_token, snapshot, revision,"
                        " status, saved_snapshot, saved_full, saved_at, updated_at)"
                        " VALUES (?, ?, ?, 1, 'saved', ?, ?, ?, ?)",
                        (full["review_id"], token,
                         json.dumps(self._snapshot_of(full), ensure_ascii=False),
                         json.dumps(self._snapshot_of(full), ensure_ascii=False),
                         json.dumps(saved_full, ensure_ascii=False),
                         recorded_at, now_iso()))
                    self._conn.execute(
                        "INSERT OR IGNORE INTO import_provenance"
                        " (source_digest, review_id, recorded_at, imported_at)"
                        " VALUES (?, ?, ?, ?)",
                        (digest, full["review_id"], recorded_at, now_iso()))
                    imported += 1
                self._conn.execute("COMMIT")
            except ImportAbort:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.IntegrityError as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise ImportAbort(
                    "import aborted on conflicting concurrent write;"
                    f" no partial data was kept: {exc}") from exc
            except sqlite3.Error as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(
                    f"import failed; rolled back: {exc}") from exc
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return {"imported": imported, "skipped_identical": skipped,
                    "source_digest": digest, "dry_run": False}
