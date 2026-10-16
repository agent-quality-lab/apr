#!/usr/bin/env python3
"""Content-free Codex command-hook collector and local review URL listing."""
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_store import ReviewStore, StoreError, ensure_private_dir, resolve_data_dir  # noqa: E402

ADAPTER_VERSION = "1"
HOOK_DB_TIMEOUT_MS = 250
HOOK_LOCK_TIMEOUT_SECONDS = 0.25


def _write_sanitized_diagnostic(data_dir):
    """Append only a fixed failure class to an owner-only, no-follow log."""
    path = os.path.join(str(resolve_data_dir(data_dir)), "capture.log")
    ensure_private_dir(os.path.dirname(path))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | nofollow, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        os.fchmod(fd, 0o600)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        os.write(fd, (stamp + " capture_error\n").encode("ascii"))
    finally:
        os.close(fd)


def _tool_category(name):
    # Inspect only to map to a bounded category; the value is discarded.
    if not isinstance(name, str):
        return "other"
    if name.startswith(("mcp__agent-performance-review__",
                        "mcp__agent_performance_review__")):
        return None
    if name in {"Bash", "Shell", "Computer"}:
        return "shell"
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def _mark_gap(data_dir, session_id, gap):
    with ReviewStore.open(data_dir, busy_timeout_ms=HOOK_DB_TIMEOUT_MS,
                          init_lock_timeout=HOOK_LOCK_TIMEOUT_SECONDS) as store:
        return store.mark_runtime_gap(session_id, gap)


def collect(payload, data_dir=None, observed_at=None, monotonic_ns=None):
    """Consume a documented hook object, reading allowlisted values only."""
    if not isinstance(payload, dict):
        raise StoreError("invalid hook payload")
    hook = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    source = payload.get("source")
    model = payload.get("model")
    mapping = {
        "SessionStart": "session_start", "UserPromptSubmit": "turn_start",
        "PreToolUse": "tool_pre", "PostToolUse": "tool_post",
        "Stop": "turn_stopped", "Interrupt": "turn_interrupted",
    }
    kind = mapping.get(hook)
    if not kind:
        return None
    if not isinstance(session_id, str) or not session_id:
        try:
            _write_sanitized_diagnostic(data_dir)
        except Exception:
            pass
        return None
    now = observed_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    mono = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    start_source = source if source in {"startup", "resume", "compact", "clear", "fork"} else "resume"
    event = {"kind": kind, "observed_at_utc": now,
             "observed_monotonic_ns": mono, "adapter_version": ADAPTER_VERSION}
    if isinstance(model, str) and model:
        event["model_slug"] = model[:128]
    if hook == "SessionStart":
        event["source"] = start_source
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        event["turn_id"] = turn_id
    if kind in {"tool_pre", "tool_post"}:
        tool_id = payload.get("tool_use_id")
        if not isinstance(tool_id, str) or not tool_id:
            try:
                if not _mark_gap(data_dir, session_id, "missing_tool_identity"):
                    _write_sanitized_diagnostic(data_dir)
            except Exception:
                try: _write_sanitized_diagnostic(data_dir)
                except Exception: pass
            return None
        category = _tool_category(payload.get("tool_name"))
        if category is None:
            return None
        event["tool_use_id"] = tool_id
        event["safe_tool_category"] = category
    if kind in {"turn_start", "turn_stopped", "turn_interrupted"} and not event.get("turn_id"):
        try:
            if not _mark_gap(data_dir, session_id, "missing_turn_identity"):
                _write_sanitized_diagnostic(data_dir)
        except Exception:
            try: _write_sanitized_diagnostic(data_dir)
            except Exception: pass
        return None
    with ReviewStore.open(data_dir, busy_timeout_ms=HOOK_DB_TIMEOUT_MS,
                          init_lock_timeout=HOOK_LOCK_TIMEOUT_SECONDS) as store:
        if kind == "session_start":
            review_id = store.ensure_runtime_job(session_id, start_source,
                                                 event.get("model_slug"), ADAPTER_VERSION)
            if start_source in {"resume", "compact"}:
                # SessionStart payloads have no documented occurrence ID. Keep
                # the lifecycle marker idempotent in the event table, but make
                # that ambiguity visible instead of treating source as unique.
                store.mark_runtime_gap(session_id, "lifecycle_occurrence_unverifiable")
        else:
            # If opt-in began after startup (or SessionStart was missed), make
            # a visibly late binding from the native ID rather than dropping
            # every later event or guessing from cwd/recency.
            store.ensure_runtime_job(session_id, "resume",
                                     event.get("model_slug"), ADAPTER_VERSION)
        result = store.ingest_runtime_event(session_id, event)
    return result if kind != "session_start" else {"review_id": review_id, **result}


def list_reviews(data_dir=None):
    from local_service import ensure_service
    base_url, _credential = ensure_service(data_dir)
    with ReviewStore.open(data_dir) as store:
        rows = store.list_runtime_reviews()
    result = []
    for row in rows:
        token = row.pop("job_token")
        result.append({**row, "rating_url": base_url + "/review/" + token + "/"})
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = None
    if argv and argv[0] == "--data-dir":
        if len(argv) < 2:
            return 0
        data_dir, argv = argv[1], argv[2:]
    command = argv[0] if argv else "capture"
    try:
        if command == "list":
            print(json.dumps(list_reviews(data_dir), ensure_ascii=False))
        else:
            # Hook stdout is intentionally empty; malformed input and store
            # failures never echo payload values or affect Codex continuation.
            payload = json.load(sys.stdin)
            try:
                collect(payload, data_dir)
            except Exception:
                session_id = payload.get("session_id") if isinstance(payload, dict) else None
                marked = False
                if isinstance(session_id, str) and session_id:
                    try:
                        with ReviewStore.open(data_dir, busy_timeout_ms=HOOK_DB_TIMEOUT_MS,
                                              init_lock_timeout=HOOK_LOCK_TIMEOUT_SECONDS) as store:
                            source = payload.get("source")
                            gap = ("identity_continuity_unsupported"
                                   if source in {"clear", "fork"} else "capture_error")
                            marked = store.mark_runtime_gap(session_id, gap)
                    except Exception:
                        pass
                if not marked:
                    try:
                        _write_sanitized_diagnostic(data_dir)
                    except Exception:
                        pass
    except Exception as exc:
        # No exception text: SQLite and hook messages can contain paths/data.
        if command == "list":
            print("review listing unavailable (" + type(exc).__name__ + ")", file=sys.stderr)
        else:
            try:
                _write_sanitized_diagnostic(data_dir)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
