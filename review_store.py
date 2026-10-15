#!/usr/bin/env python3
"""SQLite-backed review store: migrations, transactions, legacy JSONL import.

Standard library only. Depends on review_schema for validation (no cycles:
review_schema imports nothing local). Sensitive files are never symlinks and
stay mode 0600 inside a mode 0700 directory.
"""
import errno
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
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

MIGRATIONS = [(1, MIGRATION_1), (2, MIGRATION_2)]

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


def _acquire_init_lock(data_dir):
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
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
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

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.db_path = str(self.data_dir / "reviews.db")
        self._lock = threading.Lock()
        self._conn = None
        ensure_private_dir(self.data_dir)
        # Serialize the whole bootstrap across processes and threads: the
        # pre-open sidefile checks, SQLite connect/mode setup, migrations,
        # and file securing must not interleave with another cold open or
        # a concurrent close. The lock is held only here (and briefly in
        # close), never for normal long-lived operations. Lock order is
        # always init-lock outer, per-instance lock inner.
        lock_fd = _acquire_init_lock(self.data_dir)
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
                    isolation_level=None, timeout=10.0)
            except sqlite3.Error as exc:
                raise StoreError(
                    f"cannot open database {self.db_path}: {exc}") from exc
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=10000")
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
    def open(cls, data_dir=None):
        return cls(resolve_data_dir(data_dir))

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
            lock_fd = _acquire_init_lock(self.data_dir)
        except StoreError:
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

        def summarize(items):
            score_rows = [item["ratings"] for item in items]
            means = {k: round(sum(r[k] for r in score_rows) / len(score_rows), 2)
                     for k in ("overall", "quality", "speed", "resource_efficiency")} \
                if score_rows else None
            outcomes = [i.get("completion") for i in items
                        if i.get("completion") in ("yes", "no")]
            rate = round(outcomes.count("yes") / len(outcomes), 2) if outcomes else None
            return {"count": len(items), "completion_rate": rate, "mean_ratings": means}

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
