"""Validation for task-centric APR records and share projections."""
import hashlib
import json
import math
import re
from review_schema import ValidationError as ReviewValidationError


class ValidationError(ReviewValidationError):
    """Invalid task data or unsafe data that must not be persisted."""


class StaleTaskRevisionError(ValidationError):
    """Task evidence changed after the human review form was loaded."""


CATEGORIES = {"coding", "writing", "research", "other"}
SOURCES = {"user-stated", "agent-reported", "runtime-observed", "imported"}
EVENT_KINDS = {"milestone", "setup", "context", "artifact", "criteria", "resource"}
SHARE_FIELDS = {"task_text", "definitions", "context", "process", "artifacts", "resources", "setup"}
OPTIONAL_RATINGS = {"quality", "speed", "resource_efficiency"}
GAP_REASONS = {"incomplete", "missed_brief", "incorrect", "missing_tool"}
SECRET_KEY_NAMES = {"password", "passwd", "secret", "credential", "credentials",
                    "apikey", "accesskey", "privatekey", "authorization", "auth",
                    "authheader", "authorizationheader", "bearer", "cookie",
                    "setcookie", "token", "accesstoken", "refreshtoken", "idtoken",
                    "authtoken", "sessiontoken"}
SECRET_KEY_SUFFIXES = ("apikey", "accesskey", "privatekey", "secret", "credential",
                       "credentials", "accesstoken", "refreshtoken", "idtoken",
                       "authtoken", "sessiontoken", "token", "authorization",
                       "bearer", "auth")
SECRET_VALUE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|sb_secret_[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|Bearer\s+\S+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})", re.I)
MAX_JSON_BYTES = 65536


def is_credential_key(key):
    """Recognize credential fields without matching ordinary words like author."""
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (normalized in SECRET_KEY_NAMES
            or any(normalized.endswith(suffix) for suffix in SECRET_KEY_SUFFIXES))


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("data must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValidationError("data exceeds the 65536 byte limit")
    return encoded


def reject_credentials(value, trail="data"):
    """Reject credential-shaped fields and recognizable secret values only.

    This deliberately does not search narrative text or transcripts for loose
    keyword mentions.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{trail} object keys must be strings")
            if is_credential_key(key):
                raise ValidationError(f"credential-shaped key rejected at {trail}.{key}")
            reject_credentials(child, f"{trail}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_credentials(child, f"{trail}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE.search(value):
        raise ValidationError(f"obvious secret value rejected at {trail}")
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValidationError(f"non-finite number rejected at {trail}")


def _text(value, name, required=True, limit=4000):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise ValidationError(f"{name} exceeds {limit} characters")
    return value


def _strings(value, name, required=True):
    if not isinstance(value, list) or (required and not value):
        raise ValidationError(f"{name} must be a {'non-empty ' if required else ''}list of strings")
    if len(value) > 200 or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in value):
        raise ValidationError(f"{name} must contain up to 200 non-empty strings")
    return [item.strip() for item in value]


def validate_start(args):
    required = {"request_id", "title", "category", "goal", "success_criteria",
                "runtime", "model", "harness"}
    allowed = required | {"parent_task_id", "context", "baseline_missing", "baseline_source"}
    if not isinstance(args, dict) or set(args) - allowed or required - set(args):
        raise ValidationError("start_task has unknown or missing fields")
    reject_credentials(args)
    request_id = _text(args["request_id"], "request_id", limit=128)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        raise ValidationError("request_id must contain letters, digits, . _ : -")
    category = args["category"]
    if category not in CATEGORIES:
        raise ValidationError("category must be coding, writing, research, or other")
    baseline_source = args.get("baseline_source", "agent-reported")
    if baseline_source not in SOURCES:
        raise ValidationError("baseline_source must be a supported source label")
    harness = args["harness"]
    harness_fields = {"mode", "name", "version", "roles", "loops", "rules", "tools", "context_categories", "definitions"}
    if not isinstance(harness, dict) or set(harness) - harness_fields or not {"mode", "version", "roles", "loops", "rules", "tools", "context_categories"} <= set(harness):
        raise ValidationError("harness has unknown or missing fields")
    if harness["mode"] not in {"existing", "newcomer"}:
        raise ValidationError("harness.mode must be existing or newcomer")
    out_harness = {"mode": harness["mode"], "name": _text(harness.get("name", ""), "harness.name", required=False),
                   "version": _text(harness["version"], "harness.version", limit=256)}
    for key in ("roles", "loops", "rules", "tools", "context_categories"):
        out_harness[key] = _strings(harness[key], f"harness.{key}", required=False)
    definitions = harness.get("definitions", {})
    if not isinstance(definitions, dict):
        raise ValidationError("harness.definitions must be an object")
    _json(definitions)
    parent = args.get("parent_task_id")
    if parent is not None:
        parent = _text(parent, "parent_task_id", limit=128)
    context = args.get("context", {})
    if not isinstance(context, dict) or set(context) - SOURCES:
        raise ValidationError("context must be grouped by source labels")
    _json(context)
    if "baseline_missing" in args and not isinstance(args["baseline_missing"], bool):
        raise ValidationError("baseline_missing must be a boolean")
    return {"request_id": request_id, "title": _text(args["title"], "title"),
            "category": category, "goal": _text(args["goal"], "goal"),
            "success_criteria": _strings(args["success_criteria"], "success_criteria"),
            "runtime": _text(args["runtime"], "runtime", required=False, limit=512),
            "model": _text(args["model"], "model", required=False, limit=512),
            "harness": {**out_harness, "definitions": definitions},
            "parent_task_id": parent, "context": context,
            "baseline_missing": bool(args.get("baseline_missing", False)),
            "baseline_source": baseline_source}


def validate_evidence(event_id, kind, source, data):
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", event_id):
        raise ValidationError("event_id must contain letters, digits, . _ : -")
    if kind not in EVENT_KINDS:
        raise ValidationError("unsupported evidence kind")
    if source not in SOURCES:
        raise ValidationError("source must be user-stated, agent-reported, runtime-observed, or imported")
    if not isinstance(data, dict):
        raise ValidationError("evidence data must be an object")
    reject_credentials(data)
    if kind == "resource":
        if "source" in data:
            raise ValidationError("resource source is set by the event and cannot appear in data")
        for key, metric in data.items():
            if not isinstance(key, str) or not key.strip():
                raise ValidationError("resource metric names must be non-empty strings")
            if not isinstance(metric, dict) or set(metric) != {"value", "unit"}:
                raise ValidationError("resource metrics require exactly value and unit")
            value, unit = metric["value"], metric["unit"]
            if not isinstance(unit, str) or not unit.strip():
                raise ValidationError("resource metric unit must be a non-empty string")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"resource metric {key} must be numeric")
            if not math.isfinite(value) or value < 0:
                raise ValidationError(f"resource metric {key} must be finite and nonnegative")
    _json(data)
    return {"event_id": event_id, "kind": kind, "source": source, "data": data}


def validate_review_payload(payload, current_revision):
    allowed = {"completion", "ratings", "gap_reasons", "revision"}
    if not isinstance(payload, dict) or set(payload) - allowed or not allowed <= set(payload):
        raise ValidationError("review requires completion, ratings, gap_reasons, and revision")
    reject_credentials(payload)
    if payload["completion"] not in {"yes", "no"}:
        raise ValidationError("completion must be yes or no")
    ratings = payload["ratings"]
    allowed_ratings = {"overall"} | OPTIONAL_RATINGS
    if not isinstance(ratings, dict) or "overall" not in ratings or set(ratings) - allowed_ratings:
        raise ValidationError("ratings requires overall and permits optional quality, speed, resource_efficiency")
    for key, value in ratings.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValidationError(f"ratings.{key} must be an integer from 1 to 5")
    reasons = payload["gap_reasons"]
    if (not isinstance(reasons, list) or any(not isinstance(r, str) or r not in GAP_REASONS for r in reasons)
            or len(set(reasons)) != len(reasons)):
        raise ValidationError("gap_reasons must be unique choices from the supported reasons")
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != current_revision:
        raise StaleTaskRevisionError("revision is stale; reload the task and review again")
    if payload["completion"] == "yes" and reasons:
        raise ValidationError("gap_reasons are only valid when completion is no")
    return {"completion": payload["completion"], "ratings": ratings,
            "gap_reasons": reasons, "revision": revision}


def share_path(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValidationError("invalid share field path")
    parts = value.split(".")
    if parts[0] not in SHARE_FIELDS or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p) for p in parts):
        raise ValidationError("share field path is not allowed")
    return parts


def digest_payload(payload):
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
