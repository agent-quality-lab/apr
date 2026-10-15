#!/usr/bin/env python3
"""Offer a review link after every completed turn, using the active run snapshot."""
import json
import sys

from run_state import metadata


def main():
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return

    if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
        return

    try:
        run = metadata(event.get("session_id"), event.get("model"))
    except Exception:
        run = {
            "review_id": "unknown",
            "model": event.get("model") or "unknown",
            "tool_categories": {},
            "tool_calls": "unknown",
            "elapsed_time": "unknown",
            "tokens": "unknown",
            "cost": "unknown",
            "loops": "unknown",
            "roles": "unknown",
        }

    categories = run.get("tool_categories") or {}
    category_text = ", ".join(f"{name}: {count}" for name, count in categories.items()) or "none observed"
    metadata_text = (
        f"review_id={run.get('review_id', 'unknown')}; runtime=codex; agent=unknown; model={run.get('model', 'unknown')}; "
        f"tool categories={category_text}; tool calls={run.get('tool_calls', 'unknown')}; "
        f"elapsed seconds={run.get('elapsed_time', 'unknown')}; tokens=unknown; cost=unknown. "
        "The metadata is a cumulative, content-free snapshot for the current task/run. "
        "Do not read the transcript, prompts, tool arguments, tool outputs, filenames, or documents."
    )

    print(json.dumps({
        "decision": "block",
        "reason": (
            "At the end of this turn, always offer the user's review link. Invoke the "
            "agent-performance-review skill, summarize the full active task/run across its "
            "turns using only visible conversation evidence, and call request_rating with "
            "the stable review_id and cumulative metadata below. End your entire response "
            "with exactly one Markdown hyperlink whose visible text is "
            "'⭐ Review this task' and whose URL is the returned rating_url. "
            "Do this even if the task continues in a later turn. Do not open the page, "
            "ask for a rating in chat, or save a review. Do not estimate tokens or cost. "
            "Set context.runtime to codex and context.agent to unknown. Use unknown when measurements are unavailable. If link creation fails, briefly "
            "say the local review link could not be created.\n\n"
            f"Safe cumulative run metadata: {metadata_text}"
        ),
    }))


if __name__ == "__main__":
    main()
