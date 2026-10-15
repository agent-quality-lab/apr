#!/usr/bin/env python3
"""Small adapter for runtimes whose hook payloads are not Codex's exact shape.

Supported targets in this prototype: Claude Code, Cursor, and Muse Code.
Only runtime/model identifiers, broad tool categories, and timestamps are stored.
No prompt, transcript, tool arguments, or tool output is read or persisted.
"""
import json
import sys

from capture_metadata_hook import tool_category
from run_state import metadata, record_tool


RUNTIMES = {"claude-code", "cursor", "muse-code"}


def handle(event, runtime):
    if runtime not in RUNTIMES or not isinstance(event, dict):
        return

    event_name = event.get("hook_event_name", "")
    is_cursor = runtime == "cursor"
    if is_cursor:
        event_name = {"postToolUse": "PostToolUse", "stop": "Stop"}.get(event_name, event_name)

    session_id = event.get("session_id") or event.get("conversation_id") or event.get("MUSE_SESSION_ID")
    model = event.get("model") or "unknown"
    agent = event.get("agent_type") or event.get("agent_id") or "unknown"

    if event_name == "PostToolUse":
        category = tool_category(event.get("tool_name"))
        if category:
            record_tool(session_id, model, category, runtime, agent)
        return

    if event_name != "Stop":
        return
    if is_cursor:
        try:
            loop_count = int(event.get("loop_count", 0) or 0)
        except (TypeError, ValueError):
            loop_count = 0
        if event.get("status") != "completed" or loop_count > 0:
            return
    elif event.get("stop_hook_active"):
        return

    run = metadata(session_id, model, runtime, agent)
    categories = run.get("tool_categories") or {}
    category_text = ", ".join(f"{name}: {count}" for name, count in categories.items()) or "none observed"
    instruction = (
        "This task turn is complete. Invoke the agent-performance-review MCP request_rating tool once. "
        "Use short Task/Action/Result summaries grounded only in visible conversation evidence, and include "
        "the cumulative run metadata below. Set context.runtime and context.agent to the supplied values; "
        "set context.model to the supplied model. Do not read a transcript or estimate unavailable usage. "
        "Use unknown for tokens and cost when no measurement is provided. End the whole response with one "
        "Markdown link whose visible text is '⭐ Review this task' and whose URL is the returned rating_url. "
        "If link creation fails, say so briefly.\n\n"
        f"review_id={run['review_id']}; runtime={run['runtime']}; agent={run['agent']}; model={run['model']}; "
        f"tool categories={category_text}; tool calls={run['tool_calls']}; elapsed seconds={run['elapsed_time']}; "
        "tokens=unknown; cost=unknown; loops=unknown; roles=unknown."
    )
    if is_cursor:
        print(json.dumps({"followup_message": instruction}))
    else:
        print(json.dumps({"decision": "block", "reason": instruction}))


def main():
    if len(sys.argv) != 2:
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    handle(event, sys.argv[1])


if __name__ == "__main__":
    main()
