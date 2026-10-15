#!/usr/bin/env python3
"""Local Agent Performance Review MCP server (stdio, standard library only).

Thin stdio JSON-RPC bridge: every tool call is forwarded to the per-user
loopback service (local_service.py), which owns SQLite persistence and the
browser UI. stdout carries newline-delimited JSON-RPC messages only;
diagnostics go to stderr. No credentials are printed.

Prototype hook modules (capture_metadata_hook, runtime_hook, stop_hook,
run_state, hooks.json) remain uninstalled and explicitly unsupported; this
bridge does not depend on them.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_service import ensure_service, http_call  # noqa: E402
from review_schema import REVIEW_ID  # noqa: E402
from review_store import SCOPE, StoreError  # noqa: E402

SERVER_INFO = {"name": "agent-performance-review", "version": "0.5.0"}
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

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
VALUE_SCHEMA = {"type": ["string", "number"], "description": "Known value, or the string 'unknown'."}
TOOLS = [
    {
        "name": "request_rating",
        "description": "Create a clickable local rating link for the active task/run. "
                       "Repeated calls with the same review_id return the SAME link and "
                       "refresh its card to the latest snapshot; links stay valid until "
                       "the review data is explicitly removed. The page saves locally.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["review_id", "job_type", "task", "action", "result", "verification"],
            "properties": {
                "review_id": {"type": "string", "pattern": REVIEW_ID.pattern},
                "job_type": {"type": "string", "enum": JOB_TYPES},
                **{k: {"type": "string", "minLength": 1, "maxLength": MAX_TEXT, "description": d}
                   for k, d in SUMMARY_FIELDS.items()},
                "verification": {"type": "string", "enum": VERIFICATION},
                "context": {"type": "object", "additionalProperties": False,
                            "properties": {k: VALUE_SCHEMA for k in CONTEXT_KEYS}},
                "resources": {"type": "object", "additionalProperties": False,
                              "properties": {k: VALUE_SCHEMA for k in RESOURCE_KEYS}},
            },
        },
    },
    {
        "name": "record_review",
        "description": "Save one local agent performance review with a yes/no outcome and four 1-5 star ratings. "
                       "Retrying the same review_id with identical content is safe.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["review_id", "job_type", "task", "action", "result", "verification",
                         "completion", "ratings"],
            "properties": {
                "review_id": {"type": "string", "pattern": REVIEW_ID.pattern,
                              "description": "Stable caller-supplied ID for this review."},
                "job_type": {"type": "string", "enum": JOB_TYPES},
                **{k: {"type": "string", "minLength": 1, "maxLength": MAX_TEXT, "description": d}
                   for k, d in SUMMARY_FIELDS.items()},
                "verification": {"type": "string", "enum": VERIFICATION},
                "completion": {"type": "string", "enum": COMPLETION},
                "gap_reasons": {"type": "array", "items": {"type": "string", "enum": GAP_REASONS},
                                "uniqueItems": True, "maxItems": 4},
                "context": {"type": "object", "additionalProperties": False,
                            "properties": {k: VALUE_SCHEMA for k in CONTEXT_KEYS}},
                "resources": {"type": "object", "additionalProperties": False,
                              "properties": {k: VALUE_SCHEMA for k in RESOURCE_KEYS}},
                "ratings": {"type": "object", "additionalProperties": False, "required": RATINGS,
                            "properties": {k: {"type": "integer", "minimum": 1, "maximum": 5}
                                           for k in RATINGS}},
            },
        },
    },
    {
        "name": "review_summary",
        "description": "Local-only count and mean ratings from this machine's review database, "
                       "grouped by job type. Not community data.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"job_type": {"type": "string", "enum": JOB_TYPES}},
        },
    },
]


class ToolError(Exception):
    pass


class UnknownTool(Exception):
    pass


def log(message):
    print(f"agent-performance-review: {message}", file=sys.stderr, flush=True)


def service_call(endpoint, args):
    try:
        base_url, credential = ensure_service()
    except (StoreError, ConnectionError, OSError) as exc:
        raise ToolError(f"local review service is unavailable: {exc}") from exc
    try:
        status, body = http_call(base_url, "/api/internal/" + endpoint,
                                 credential=credential,
                                 payload={"arguments": args}, timeout=15)
    except ConnectionError as exc:
        raise ToolError(f"local review service is unreachable: {exc}") from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError(f"local review service returned an unreadable reply: {exc}") from exc
    if status == 200:
        return data
    detail = data.get("error") if isinstance(data, dict) else None
    raise ToolError(detail or f"local review service failed (HTTP {status})")


def request_rating(args):
    return service_call("request_rating", args)


def record_review(args):
    return service_call("record_review", args)


def review_summary(args):
    return service_call("summary", args)


HANDLERS = {"request_rating": request_rating, "record_review": record_review,
            "review_summary": review_summary}


def handle(method, params):
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        if name not in HANDLERS:
            raise UnknownTool(f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        try:
            data = HANDLERS[name](args)
        except ToolError as error:
            return {"content": [{"type": "text", "text": f"Error: {error}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data, "isError": False}
    raise NotImplementedError(method)


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def error(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            error(None, -32700, "Parse error")
            continue
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                continue  # response to a server request; this server sends none
            error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
            continue
        method, msg_id, is_request = message["method"], message.get("id"), "id" in message
        params = message.get("params") or {}
        if not is_request:
            if method not in ("notifications/initialized", "notifications/cancelled"):
                log(f"ignoring notification {method}")
            continue
        if not isinstance(params, dict):
            error(msg_id, -32602, "params must be an object")
            continue
        try:
            send({"jsonrpc": "2.0", "id": msg_id, "result": handle(method, params)})
        except NotImplementedError:
            error(msg_id, -32601, f"Method not found: {method}")
        except UnknownTool as exc:
            error(msg_id, -32602, str(exc))
        except Exception as exc:  # keep the server alive; details to stderr only
            log(f"internal error handling {method}: {exc!r}")
            error(msg_id, -32603, "Internal error")


if __name__ == "__main__":
    main()
