"""Task-centric lifecycle repository backed by ReviewStore's SQLite database."""
import json
import math
import re
import secrets
from datetime import datetime, timezone

from task_schema import (SHARE_FIELDS, ValidationError,
                         SECRET_VALUE, digest_payload, is_credential_key,
                         StaleTaskRevisionError,
                         reject_credentials, share_path,
                         validate_evidence, validate_review_payload,
                         validate_start)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class TaskStore:
    def __init__(self, review_store):
        self.store = review_store
        self.db = review_store._conn
        self._backfill_legacy()

    def _backfill_legacy(self):
        """Project legacy ratings into clearly marked tasks with no invented baseline."""
        with self.store._lock:
            rows = self.db.execute("SELECT review_id FROM jobs WHERE status='saved'").fetchall()
            for row in rows:
                review_id = row[0]
                task_id = "legacy:" + review_id
                if self.db.execute("SELECT 1 FROM task_records WHERE task_id=?", (task_id,)).fetchone():
                    continue
                card = self.store.get_card_by_review_id(review_id)
                saved = (card or {}).get("saved") or {}
                snapshot = saved.get("snapshot") or (card or {}).get("snapshot") or {}
                snapshot = _safe_legacy(snapshot)
                review = None
                if saved.get("ratings"):
                    review = {"completion": saved.get("completion"),
                              "ratings": saved["ratings"], "gap_reasons": [],
                              "recorded_at": saved.get("recorded_at"),
                              "legacy": True}
                status = "reviewed" if review else "awaiting_review"
                task = {"task_id": task_id, "title": snapshot.get("task") or "Legacy review",
                        "category": snapshot.get("job_type", "other"),
                        "goal": snapshot.get("task", ""), "success_criteria": [],
                        "runtime": (snapshot.get("context") or {}).get("runtime"),
                        "model": (snapshot.get("context") or {}).get("model"),
                        "harness": None,
                        "context": {"imported": {"context": snapshot.get("context", {}),
                                                  "action": snapshot.get("action", "")}},
                        "baseline_missing": True, "baseline_source": "imported",
                        "parent_task_id": None, "status": status,
                        "result": snapshot.get("result"), "revision": 1,
                        "evidence": [{"event_id": "legacy:" + review_id,
                                      "kind": "context", "source": "imported",
                                      "data": {"action": snapshot.get("action", ""),
                                               "context": snapshot.get("context", {}),
                                               "resources": snapshot.get("resources", {}),
                                               "verification": snapshot.get("verification")}}],
                        "post_review_evidence": [], "review": review,
                        "resources": ({"legacy-import": {"source": "imported",
                                                           **snapshot.get("resources", {})}}
                                      if snapshot.get("resources") else {}),
                        "created_at": saved.get("recorded_at") or _now(),
                        "updated_at": saved.get("recorded_at") or _now(),
                        }
                self.db.execute("INSERT OR IGNORE INTO task_records"
                                " (task_id,request_id,task_json,created_at,updated_at)"
                                " VALUES(?,?,?,?,?)",
                                (task_id, task_id, _dumps(task), task["created_at"], task["updated_at"]))
                if review:
                    self.db.execute("INSERT OR IGNORE INTO task_reviews"
                                    " (task_id,review_json,recorded_at) VALUES(?,?,?)",
                                    (task_id, _dumps(review), review["recorded_at"] or _now()))

    def _load(self, task_id):
        _validate_task_id(task_id)
        row = self.db.execute("SELECT task_json FROM task_records WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, task):
        task["updated_at"] = _now()
        self.db.execute("UPDATE task_records SET task_json=?,updated_at=? WHERE task_id=?",
                        (_dumps(task), task["updated_at"], task["task_id"]))

    def _with_transaction(self, operation):
        with self.store._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = operation()
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def start_task(self, args):
        clean = validate_start(args)
        now = _now()
        def run():
            row = self.db.execute("SELECT task_id,task_json FROM task_records WHERE request_id=?",
                                  (clean["request_id"],)).fetchone()
            if row:
                old = json.loads(row[1])
                original = {k: old.get(k) for k in clean}
                if _dumps(original) != _dumps(clean):
                    raise ValidationError("request_id was already used with different task data")
                return self._result(old)
            parent = clean["parent_task_id"]
            if parent and self._load(parent) is None:
                raise ValidationError("parent_task_id must reference an existing task")
            task_id = "task_" + secrets.token_urlsafe(18)
            task = {**clean, "task_id": task_id, "status": "in_progress",
                    "result": None, "revision": 1, "evidence": [],
                    "post_review_evidence": [],
                    "review": None, "resources": {}, "created_at": now,
                    "updated_at": now}
            self.db.execute("INSERT INTO task_records VALUES(?,?,?,?,?)",
                            (task_id, clean["request_id"], _dumps(task), now, now))
            return self._result(task)
        return self._with_transaction(run)

    def append_evidence(self, task_id, event_id, kind, source, data):
        _validate_task_id(task_id)
        event = validate_evidence(event_id, kind, source, data)
        payload = _dumps(event)
        def run():
            task = self._load(task_id)
            if task is None:
                raise ValidationError("task not found")
            existing = self.db.execute("SELECT payload_json FROM task_events WHERE task_id=? AND event_id=?",
                                       (task_id, event_id)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValidationError("event_id was already used with different evidence")
                return {"task_id": task_id, "event_id": event_id,
                        "status": "already_recorded", "revision": task["revision"]}
            now = _now()
            self.db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?)",
                            (task_id, event_id, kind, source, payload, now))
            event["created_at"] = now
            if task.get("review") is not None:
                task.setdefault("post_review_evidence", []).append(event)
            else:
                task["evidence"].append(event)
            if kind == "resource" and task.get("review") is None:
                task["resources"][event_id] = {**data, "source": source}
            # Criteria are evidence revisions; the starting success criteria stay immutable.
            if task.get("review") is None:
                task["revision"] += 1
            self._save(task)
            return {"task_id": task_id, "event_id": event_id,
                    "status": "recorded", "revision": task["revision"]}
        return self._with_transaction(run)

    def finish_task(self, task_id, event_id, result, interrupted=False, verification=None):
        _validate_task_id(task_id)
        if not isinstance(result, (str, dict)) or (isinstance(result, str) and not result.strip()):
            raise ValidationError("result must be non-empty text or an object")
        if isinstance(interrupted, bool) is False:
            raise ValidationError("interrupted must be a boolean")
        reject_credentials(result)
        if verification is not None:
            if not isinstance(verification, dict):
                raise ValidationError("verification must be an object")
            reject_credentials(verification)
        event = validate_evidence(event_id, "milestone", "agent-reported",
                                  {"result": result, "verification": verification,
                                   "interrupted": interrupted})
        payload = _dumps(event)
        def run():
            task = self._load(task_id)
            if task is None:
                raise ValidationError("task not found")
            existing = self.db.execute(
                "SELECT payload_json FROM task_events WHERE task_id=? AND event_id=?",
                (task_id, event_id)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValidationError("event_id was already used with different evidence")
                return self._result(task)
            now = _now()
            self.db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?)",
                            (task_id, event_id, "milestone", "agent-reported", payload, now))
            event["created_at"] = now
            if task.get("review") is not None:
                task.setdefault("post_review_evidence", []).append(event)
            else:
                task["evidence"].append(event)
                task["revision"] += 1
            if task.get("review") is not None:
                task["status"] = "reviewed"
            else:
                task["result"] = result
                task["verification"] = verification
                task["status"] = "interrupted" if interrupted else "awaiting_review"
            self._save(task)
            return self._result(task)
        return self._with_transaction(run)

    def get_task(self, task_id):
        _validate_task_id(task_id)
        with self.store._lock:
            task = self._load(task_id)
            return self._result(task) if task else None

    def list_tasks(self, status=None):
        if status is not None and (not isinstance(status, str) or status not in {
                "in_progress", "interrupted", "awaiting_review", "reviewed"}):
            raise ValidationError("unsupported task status filter")
        with self.store._lock:
            rows = self.db.execute("SELECT task_json FROM task_records ORDER BY created_at DESC").fetchall()
            items = [self._list_summary(json.loads(row[0])) for row in rows]
            return [item for item in items if status is None or item["status"] == status]

    def save_review(self, task_id, payload):
        _validate_task_id(task_id)
        def run():
            task = self._load(task_id)
            if task is None:
                raise ValidationError("task not found")
            if task["status"] not in {"awaiting_review", "interrupted", "reviewed"}:
                raise ValidationError("task must be finished before human review")
            review = validate_review_payload(payload, task["revision"])
            existing = self.db.execute("SELECT review_json FROM task_reviews WHERE task_id=?", (task_id,)).fetchone()
            data = {k: v for k, v in review.items() if k != "revision"}
            if existing:
                old = json.loads(existing[0])
                if {k: v for k, v in old.items() if k != "recorded_at"} == data:
                    return self._result(task)
                raise ValidationError("human review is already saved and cannot be overwritten")
            now = _now()
            review = {**data, "recorded_at": now}
            task["reviewed_evidence_count"] = len(task["evidence"])
            self.db.execute("INSERT INTO task_reviews VALUES(?,?,?)", (task_id, _dumps(review), now))
            task["review"] = review
            task["status"] = "reviewed"
            self._save(task)
            return self._result(task)
        return self._with_transaction(run)

    def setup_get(self):
        with self.store._lock:
            row = self.db.execute("SELECT mode,runtime FROM task_setup WHERE singleton=1").fetchone()
            if row:
                mode, runtime = row
            else:
                mode, runtime = "existing", "Codex"
            return {"mode": mode, "runtime": runtime,
                    "capabilities": {"task_lifecycle": "available", "runtime_capture": "manual",
                                     "installation": "not_performed"}}

    def setup_save(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"mode", "runtime"}:
            raise ValidationError("setup requires mode and runtime")
        reject_credentials(payload)
        if payload["mode"] not in {"existing", "newcomer"}:
            raise ValidationError("mode must be existing or newcomer")
        if not isinstance(payload["runtime"], str) or not payload["runtime"].strip() or len(payload["runtime"]) > 512:
            raise ValidationError("runtime must be a non-empty string")
        with self.store._lock:
            self.db.execute("INSERT OR REPLACE INTO task_setup VALUES(1,?,?,?)",
                            (payload["mode"], payload["runtime"].strip(), _now()))
        return self.setup_get()

    def prepare_share(self, task_id, selected_fields=None, redactions=None):
        task = self.get_task(task_id)
        if task is None:
            raise ValidationError("task not found")
        if task["review"] is None:
            raise ValidationError("a saved human review is required before sharing")
        selected_fields = selected_fields or []
        if (not isinstance(selected_fields, list)
                or any(not isinstance(value, str) for value in selected_fields)
                or len(set(selected_fields)) != len(selected_fields)):
            raise ValidationError("selected_fields must be a unique list")
        paths = [share_path(path) for path in selected_fields]
        redactions = redactions or {}
        if not isinstance(redactions, dict):
            raise ValidationError("redactions must be an object")
        redaction_paths = {".".join(share_path(k)): v for k, v in redactions.items()}
        if any(not isinstance(value, str) for value in redactions.values()):
            raise ValidationError("redaction replacements must be strings")
        reject_credentials(redactions)
        h = task["harness"] or {}
        roots = {
            "task_text": {"title": task["title"], "goal": task["goal"],
                          "success_criteria": task["success_criteria"],
                          "result": task.get("result")},
            "definitions": (task["harness"] or {}).get("definitions", {}),
            "context": {"baseline": task["context"],
                        "evidence": [{"source": e["source"], "data": e["data"]}
                                     for e in task["evidence"] if e["kind"] == "context"]},
            "process": [{"kind": e["kind"], "source": e["source"], "data": e["data"]}
                        for e in task["evidence"] if e["kind"] in {"milestone", "setup", "criteria"}],
            "artifacts": [{"data": e["data"], "source": e["source"]}
                          for e in task["evidence"] if e["kind"] == "artifact"],
            "resources": list(task["resources"].values()),
            "setup": {"runtime": task["runtime"], "model": task["model"],
                      "harness": {key: h.get(key) for key in
                                  ("name", "mode", "version", "roles", "loops", "rules", "tools", "context_categories")}},
        }
        details = {}
        for path in paths:
            root = path[0]
            if root not in details:
                details[root] = roots[root] if len(path) == 1 else ([] if isinstance(roots[root], list) else {})
            if len(path) > 1:
                value = _get_path(roots[root], path[1:])
                if value is not None:
                    _set_leaf(details[root], path[1:], value)
        for key, replacement in redaction_paths.items():
            parts = key.split(".")
            if parts[0] in details:
                _set_existing(details[parts[0]], parts[1:], replacement)
        details = _sanitize_share(details)
        process_sources = {e["source"] for e in task["evidence"]
                           if e["kind"] in {"milestone", "setup", "criteria"}}
        process_coverage = ("mixed" if {"agent-reported", "runtime-observed"} <= process_sources
                            else "agent-reported" if "agent-reported" in process_sources
                            else "runtime-observed" if "runtime-observed" in process_sources
                            else "unavailable")
        payload = {"schema_version": "apr.share.v1", "category": task["category"],
                   "completion": task["review"]["completion"],
                   "ratings": dict(task["review"]["ratings"]),
                   "harness": {"mode": h.get("mode", "existing"),
                               "version": h.get("version", "unknown"),
                               "structure": {"roles": len(h.get("roles", [])),
                                             "loops": len(h.get("loops", [])),
                                             "rules": len(h.get("rules", [])),
                                             "tools": len(h.get("tools", [])),
                                             "context_categories": len(h.get("context_categories", []))}},
                   "coverage": {"baseline": "missing" if task["baseline_missing"] else "recorded",
                                "process": process_coverage,
                                "resources": "measured" if any(v.get("source") == "runtime-observed" for v in task["resources"].values()) else "reported" if task["resources"] else "unavailable"},
                   "details": details, "withheld": sorted(SHARE_FIELDS - set(details))}
        reject_credentials(payload)
        encoded = _dumps(payload)
        preview_id = "share_" + secrets.token_urlsafe(16)
        digest = digest_payload(payload)
        created_at = _now()
        with self.store._lock:
            self.db.execute("INSERT INTO task_share_previews VALUES(?,?,?,?,?)",
                            (preview_id, task_id, encoded, digest, created_at))
        return {"preview_id": preview_id, "payload": payload, "digest": digest,
                "created_at": created_at}

    def get_share_preview(self, preview_id):
        if not isinstance(preview_id, str) or not re.fullmatch(r"share_[A-Za-z0-9_-]{8,128}", preview_id):
            raise ValidationError("invalid share preview id")
        with self.store._lock:
            row = self.db.execute("SELECT payload_json,digest,created_at FROM task_share_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if not row:
                return None
            return {"preview_id": preview_id, "payload": json.loads(row[0]),
                    "digest": row[1], "created_at": row[2]}

    @staticmethod
    def _result(task):
        if task is None:
            return None
        return {k: v for k, v in task.items() if k != "legacy_action"}

    @staticmethod
    def _list_summary(task):
        harness = task.get("harness") or {}
        review = task.get("review")
        if review is not None:
            review = {key: review.get(key) for key in
                      ("completion", "ratings", "recorded_at")}
        resources = {}
        for index, record in enumerate(task.get("resources", {}).values()):
            source = record.get("source") if isinstance(record, dict) else None
            if not isinstance(record, dict):
                continue
            concise = {}
            for name, metric in record.items():
                if name == "source":
                    continue
                if isinstance(metric, dict) and set(metric) == {"value", "unit"}:
                    concise[name] = metric
                elif (isinstance(metric, (int, float))
                      and not isinstance(metric, bool)
                      and math.isfinite(metric) and metric >= 0):
                    # Legacy imported values have unknown units; preserve that uncertainty.
                    concise[name] = {"value": metric, "unit": ""}
            if concise:
                resources[f"resource_{index}"] = {**concise, "source": source}
        return {"task_id": task["task_id"], "title": task["title"],
                "category": task["category"], "status": task["status"],
                "runtime": task.get("runtime"), "model": task.get("model"),
                "harness": ({key: harness.get(key) for key in ("mode", "name", "version")}
                            if task.get("harness") is not None else None),
                "review": review, "resources": resources,
                "created_at": task["created_at"], "updated_at": task["updated_at"],
                "baseline_missing": task["baseline_missing"],
                "baseline_source": task.get("baseline_source", "imported"),
                "parent_task_id": task.get("parent_task_id"),
                "evidence_count": (len(task.get("evidence", []))
                                    + len(task.get("post_review_evidence", [])))}


def _set_leaf(target, path, value):
    _write_path(target, path, value, create=True)


def _set_existing(target, path, value):
    _write_path(target, path, value, create=False)


def _get_path(value, path):
    current = value
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _write_path(target, path, value, create):
    if not path:
        return
    current = target
    for index, part in enumerate(path):
        last = index == len(path) - 1
        next_is_index = not last and path[index + 1].isdigit()
        if isinstance(current, dict):
            if last:
                if create or part in current:
                    current[part] = value
                return
            if part not in current:
                if not create:
                    return
                current[part] = [] if next_is_index else {}
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            slot = int(part)
            if slot > 10000:
                return
            if slot >= len(current):
                if not create:
                    return
                current.extend([None] * (slot + 1 - len(current)))
            if last:
                if create or current[slot] is not None:
                    current[slot] = value
                return
            if current[slot] is None:
                if not create:
                    return
                current[slot] = [] if next_is_index else {}
            current = current[slot]
        else:
            return


def _validate_task_id(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", task_id):
        raise ValidationError("task_id must be a valid task identifier")


_LOCAL_PATH = re.compile(r"(?:^|\s)(?:/Users/|/home/|/private/|/Volumes/|[A-Za-z]:\\)")
_COMMUNITY_PRIVATE_KEY_BOUNDARY = re.compile(
    r"(?:^|_)(?:local|private|owner|user|email|secret|password|credential|authorization|authcode|session|reviewid|taskid|eventid|requestid|filepath|path)(?:_|$)",
    re.I)
_COMMUNITY_PRIVATE_KEY_COMPACT = re.compile(
    r"(?:apikey|secret|password|credential|authorization|authcode|session|email|ownerid|userid|local(?:[a-z0-9]*(?:id|uuid|key))?|reviewid|taskid|eventid|requestid|filepath|accountid|accountuuid|agentid)",
    re.I)
_COMMUNITY_ALLOWED_TOKEN_KEYS = {
    "tokens", "tokencount", "tokenused", "tokeninput", "tokenoutput", "tokentotal",
    "tokenscount", "tokensused", "tokensinput", "tokensoutput", "tokenstotal",
    "inputtokens", "outputtokens", "totaltokens",
}


def _is_community_private_key(key):
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    # Runtime/service authentication handles are local identifiers even though
    # the public validator's generic predicate does not classify these names.
    if compact.startswith(("serviceauth", "runtimeauth")):
        return True
    if _COMMUNITY_PRIVATE_KEY_BOUNDARY.search(key):
        return True
    if (_COMMUNITY_PRIVATE_KEY_COMPACT.search(compact)
            or compact in {"id", "uuid", "auth", "oauth", "path", "account"}):
        return True
    return "token" in compact and compact not in _COMMUNITY_ALLOWED_TOKEN_KEYS


def _sanitize_share(value, key=None):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _is_community_private_key(key):
                continue
            result[key] = _sanitize_share(child, key)
        return result
    if isinstance(value, list):
        return [_sanitize_share(v) for v in value]
    if isinstance(value, str) and _LOCAL_PATH.search(value):
        return "[local path withheld]"
    return value


def _safe_legacy(value):
    if isinstance(value, dict):
        return {k: _safe_legacy(v) for k, v in value.items()
                if isinstance(k, str) and not is_credential_key(k)}
    if isinstance(value, list):
        return [_safe_legacy(v) for v in value]
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return "[redacted imported value]"
    return value
