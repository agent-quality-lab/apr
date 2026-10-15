#!/usr/bin/env python3
"""SQLite-backed review store: migrations, transactions, legacy JSONL import.

Standard library only. Depends on review_schema for validation (no cycles:
review_schema imports nothing local). Sensitive files are never symlinks and
stay mode 0600 inside a mode 0700 directory.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from review_schema import ValidationError, validate_review

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


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    #: SQLite sidecar files SQLite may create beside the database.
    DB_SUFFIXES = ("", "-wal", "-shm", "-journal")

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.db_path = str(self.data_dir / "reviews.db")
        self._lock = threading.Lock()
        ensure_private_dir(self.data_dir)
        # Fail closed BEFORE any SQLite open: connecting first would let
        # SQLite read or create files through a symlinked target, and would
        # apply PRAGMAs/migrations before the sensitive-file check runs.
        self._check_db_files_preopen()
        new_db = not os.path.exists(self.db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     isolation_level=None, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        if new_db:
            try:
                os.chmod(self.db_path, 0o600)
            except OSError as exc:
                self._conn.close()
                raise StoreError(
                    f"cannot secure new database {self.db_path}: {exc}") from exc
        self.apply_migrations()
        self._secure_db_files()

    def _check_db_files_preopen(self):
        """Validate every existing database sidecar before SQLite opens any.

        Refuses symlinks and non-regular files, and enforces 0600 on what
        already exists, so no SQLite call ever touches an unsafe target.
        Only exact-path `islink` checks are used: a data directory that
        merely lives under an aliased parent (e.g. macOS /tmp) is unaffected.
        """
        for suffix in self.DB_SUFFIXES:
            candidate = self.db_path + suffix
            if os.path.islink(candidate):
                raise StoreError(
                    f"refusing symlinked database file: {candidate}")
            if os.path.lexists(candidate) and not os.path.isfile(candidate):
                raise StoreError(
                    f"database path is not a regular file: {candidate}")
            if os.path.isfile(candidate):
                try:
                    os.chmod(candidate, 0o600)
                except OSError as exc:
                    raise StoreError(
                        f"cannot secure database file {candidate}: {exc}"
                    ) from exc

    @classmethod
    def open(cls, data_dir=None):
        return cls(resolve_data_dir(data_dir))

    def _secure_db_files(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = self.db_path + suffix
            if os.path.islink(candidate):
                raise StoreError(
                    f"refusing symlinked database file: {candidate}")
            try:
                if os.path.isfile(candidate):
                    os.chmod(candidate, 0o600)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise StoreError(
                    f"cannot secure database file {candidate}: {exc}") from exc

    def close(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._secure_db_files()

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
        with self._lock:
            existing = set(self.migration_versions())
            for version, sql in MIGRATIONS:
                if version in existing:
                    continue
                with self._conn:  # transactional DDL
                    self._conn.executescript(sql)
                    self._conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at)"
                        " VALUES (?, ?)", (version, now_iso()))

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
        with self._lock:
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
                return {"imported": len(planned),
                        "skipped_identical": skipped,
                        "source_digest": digest, "dry_run": True}
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return {"imported": imported, "skipped_identical": skipped,
                    "source_digest": digest, "dry_run": False}
