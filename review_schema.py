#!/usr/bin/env python3
"""Shared review validation for the local agent-performance-review service.

Standard library only. No service, store, or MCP imports (avoids cycles).
Preserves the accepted tool fields; fixes: strict skip, non-finite rejection.
"""
import json
import math
import re
from datetime import datetime

JOB_TYPES = ["writing", "research", "coding", "other"]
VERIFICATION = ["verified", "partial", "unverified", "unknown"]
RATINGS = ["overall", "quality", "speed", "resource_efficiency"]
CONTEXT_KEYS = ["runtime", "agent", "model", "connections", "loops", "roles"]
RESOURCE_KEYS = ["elapsed_time", "tokens", "cost", "tool_calls"]
COMPLETION = ["yes", "no"]
GAP_REASONS = ["incomplete", "missed_brief", "incorrect", "missing_tool"]
SUMMARY_FIELDS = {"task": "What the user needed done.",
                  "action": "What the agent actually did.",
                  "result": "What was delivered."}
MAX_TEXT = 500
MAX_VALUE = 200
REVIEW_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RUNTIME_KINDS = {"session_start", "turn_start", "tool_pre", "tool_post",
                 "turn_stopped", "turn_interrupted", "unattributed_tool_pre",
                 "unattributed_tool_post"}
TOOL_CATEGORIES = {"shell", "mcp", "other"}


class ValidationError(ValueError):
    """Raised for any invalid review/skip/payload input."""


def parse_json_strict(raw):
    """Parse request bytes; reject NaN/Infinity and non-object roots."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    def _reject(value):
        raise ValidationError(f"non-finite number is not allowed: {value}")
    try:
        return json.loads(raw, parse_constant=_reject)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc


def check_keys(obj, name, allowed, required=()):
    if not isinstance(obj, dict):
        raise ValidationError(f"{name} must be an object")
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ValidationError(f"{name} has unknown fields: {', '.join(extra)}")
    missing = [k for k in required if k not in obj]
    if missing:
        raise ValidationError(f"{name} is missing: {', '.join(missing)}")


def check_values(obj, name, keys):
    obj = {} if obj is None else obj
    check_keys(obj, name, keys)
    out = {}
    for key in keys:
        value = obj.get(key, "unknown")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValidationError(f"{name}.{key} must be a string or number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(f"{name}.{key} must be finite")
        if isinstance(value, (int, float)) and value < 0:
            raise ValidationError(f"{name}.{key} cannot be negative")
        if isinstance(value, str):
            value = value.strip() or "unknown"
            if len(value) > MAX_VALUE:
                raise ValidationError(f"{name}.{key} exceeds {MAX_VALUE} characters")
        out[key] = value
    return out


def validate_review_context(args):
    fields = ["review_id", "job_type", *SUMMARY_FIELDS, "verification",
              "context", "resources"]
    check_keys(args, "arguments", fields,
               ["review_id", "job_type", *SUMMARY_FIELDS, "verification"])
    review_id = args["review_id"]
    if not isinstance(review_id, str) or not REVIEW_ID.match(review_id):
        raise ValidationError("review_id must be 1-128 characters of letters, digits, . _ : -")
    if args["job_type"] not in JOB_TYPES:
        raise ValidationError(f"job_type must be one of: {', '.join(JOB_TYPES)}")
    if args["verification"] not in VERIFICATION:
        raise ValidationError(f"verification must be one of: {', '.join(VERIFICATION)}")
    review = {"review_id": review_id, "job_type": args["job_type"]}
    for key in SUMMARY_FIELDS:
        value = args[key]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} must be a non-empty string")
        if len(value) > MAX_TEXT:
            raise ValidationError(
                f"{key} exceeds {MAX_TEXT} characters; send a short summary, not a transcript")
        review[key] = value.strip()
    review["verification"] = args["verification"]
    review["context"] = check_values(args.get("context"), "context", CONTEXT_KEYS)
    review["resources"] = check_values(args.get("resources"), "resources", RESOURCE_KEYS)
    return review


def validate_ratings(ratings):
    check_keys(ratings, "ratings", RATINGS, RATINGS)
    for key in RATINGS:
        value = ratings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValidationError(f"ratings.{key} must be an integer from 1 to 5")
    return {k: ratings[k] for k in RATINGS}


def validate_review(args):
    fields = ["review_id", "job_type", *SUMMARY_FIELDS, "verification", "completion",
              "gap_reasons", "context", "resources", "ratings"]
    check_keys(args, "arguments", fields,
               ["review_id", "job_type", *SUMMARY_FIELDS, "verification",
                "completion", "ratings"])
    review = validate_review_context({k: args[k] for k in fields if k in args
                                      and k in {"review_id", "job_type", *SUMMARY_FIELDS,
                                                "verification", "context", "resources"}})
    if args["completion"] not in COMPLETION:
        raise ValidationError("completion must be yes or no")
    review["completion"] = args["completion"]
    reasons = args.get("gap_reasons", [])
    if not isinstance(reasons, list) or len(reasons) > 4:
        raise ValidationError("gap_reasons must be a list with at most four choices")
    if (any(not isinstance(reason, str) or reason not in GAP_REASONS for reason in reasons)
            or len(set(reasons)) != len(reasons)):
        raise ValidationError(
            f"gap_reasons must be unique choices from: {', '.join(GAP_REASONS)}")
    if review["completion"] == "yes" and reasons:
        raise ValidationError("gap_reasons are only valid when completion is no")
    review["gap_reasons"] = reasons
    review["ratings"] = validate_ratings(args["ratings"])
    return review


def validate_skip(payload):
    """Accept exactly {"skip": True}; reject numeric/truthy variants and extras."""
    if not isinstance(payload, dict) or set(payload) != {"skip"}:
        raise ValidationError("skip payload must be exactly {\"skip\": true}")
    if payload["skip"] is not True:
        raise ValidationError("skip must be the boolean true")
    return True


def validate_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("revision must be a positive integer")
    return value


def validate_runtime_event(event):
    """Strict content-free projection accepted from the native adapter."""
    allowed = {"kind", "turn_id", "tool_use_id", "model_slug", "safe_tool_category",
               "observed_at_utc", "observed_monotonic_ns", "clock_epoch", "source",
               "adapter_version"}
    check_keys(event, "runtime event", allowed, ("kind", "observed_at_utc",
                "observed_monotonic_ns", "adapter_version"))
    if event["kind"] not in RUNTIME_KINDS:
        raise ValidationError("unsupported runtime event kind")
    out = dict(event)
    for key in ("turn_id", "tool_use_id"):
        value = out.get(key)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 512):
            raise ValidationError("invalid native event identifier")
    for key in ("model_slug", "source", "clock_epoch", "adapter_version"):
        value = out.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 128 or
                                  any(ord(ch) < 32 for ch in value)):
            raise ValidationError("invalid runtime metadata")
    if out.get("source") not in {None, "startup", "resume", "compact", "clear", "fork"}:
        raise ValidationError("unsupported runtime source")
    if out.get("safe_tool_category") not in {None, *TOOL_CATEGORIES}:
        raise ValidationError("unsupported safe tool category")
    if not isinstance(out["observed_at_utc"], str):
        raise ValidationError("invalid event timestamp")
    try:
        datetime.fromisoformat(out["observed_at_utc"])
    except ValueError as exc:
        raise ValidationError("invalid event timestamp") from exc
    mono = out["observed_monotonic_ns"]
    if isinstance(mono, bool) or not isinstance(mono, int) or mono < 0:
        raise ValidationError("invalid monotonic timestamp")
    if not isinstance(out["adapter_version"], str) or not out["adapter_version"]:
        raise ValidationError("invalid adapter version")
    if out["kind"] in {"turn_start", "turn_stopped", "turn_interrupted"} and not out.get("turn_id"):
        raise ValidationError("turn event requires turn_id")
    if out["kind"] in {"tool_pre", "tool_post"} and not out.get("tool_use_id"):
        raise ValidationError("tool event requires tool_use_id")
    return out
