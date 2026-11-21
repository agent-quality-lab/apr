"""Read numeric receipts for explicitly selected, completed local Codex turns."""
import json
import os
from pathlib import Path
import re
import sqlite3

UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
MAX_BYTES = 32 * 1024 * 1024
TOKEN_KEYS = {"tokens": "total_tokens", "input_tokens": "input_tokens",
              "cached_input_tokens": "cached_input_tokens", "output_tokens": "output_tokens"}


def selected_turn_metrics(scope, home=None):
    if not isinstance(scope, dict):
        return None
    thread = scope.get("thread_id")
    turns = scope.get("turn_ids")
    if (not isinstance(thread, str) or not UUID.fullmatch(thread)
            or not isinstance(turns, list) or not 1 <= len(turns) <= 100
            or any(not isinstance(t, str) or not UUID.fullmatch(t) for t in turns)
            or len(set(turns)) != len(turns)):
        return None
    home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        db = sqlite3.connect((home / "state_5.sqlite").absolute().as_uri() + "?mode=ro",
                             uri=True, timeout=0.2)
        try:
            row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread,)).fetchone()
        finally:
            db.close()
        if not row:
            return None
        path = Path(row[0]).resolve()
        if not (path.is_relative_to((home / "sessions").resolve())
                and path.name.endswith("-" + thread + ".jsonl") and path.is_file()):
            return None
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - MAX_BYTES)
            stream.seek(start)
            if start:
                stream.readline(MAX_BYTES)
            return read_turns(stream, thread, set(turns))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def read_turns(stream, thread, selected):
    active = None
    finished = {}
    for line in stream:
        # Skip prompts, model messages and tool outputs without decoding their content.
        header = line[:256].partition(b'"payload":')[0]
        if b'"type":"token_usage_record"' in header:
            p = json.loads(line)["payload"]
            if active and p.get("thread_id") == thread and p.get("turn_id") == active:
                usage = p.get("turn_token_usage")
                if isinstance(usage, dict):
                    totals = {key: usage.get(name) for key, name in TOKEN_KEYS.items()}
            continue
        if b'"type":"event_msg"' in header:
            if not any(marker in line[:256] for marker in (b'"type":"task_started"', b'"type":"task_complete"')):
                continue
            p = json.loads(line)["payload"]
            if p.get("type") == "task_started":
                active = p.get("turn_id") if p.get("turn_id") in selected else None
                calls, totals = set(), {}
            elif active and p.get("type") == "task_complete" and p.get("turn_id") == active:
                finished[active] = {**totals, "tool_calls": len(calls), "elapsed_time": p.get("duration_ms")}
                active = None
                if finished.keys() == selected:
                    break
        elif active and b'"type":"response_item"' in header and any(
                marker in line[:256] for marker in (b'"type":"custom_tool_call"', b'"type":"function_call"')):
            p = json.loads(line)["payload"]
            if p.get("type") in {"custom_tool_call", "function_call"} and isinstance(p.get("call_id"), str):
                calls.add(p["call_id"])
    if finished.keys() != selected:
        return None
    result = {}
    for key, unit in {**dict.fromkeys(TOKEN_KEYS, "tokens"), "tool_calls": "calls",
                      "elapsed_time": "ms"}.items():
        values = [v.get(key) for v in finished.values()]
        if all(type(n) is int and n >= 0 for n in values):
            result[key] = {"value": sum(values), "unit": unit}
    return result
