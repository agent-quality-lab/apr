"""Private Codex context receipts. These are not agent-writable task evidence."""
import re
from task_schema import ValidationError


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def inspector_event_id(data):
    # The observer owns the supported coverage vocabulary and file limits.
    from codex_context import GAP_CODES, MAX_FILES, MAX_FILE_BYTES, valid_relative_path
    if not isinstance(data, dict):
        raise ValidationError("inspector receipt must be an object")
    if data.get("type") == "inventory":
        if set(data) != {"type", "root_id", "items"} or not _digest(data["root_id"]):
            raise ValidationError("invalid inspector inventory")
        items = data["items"]
        event_id = "inspector.inventory"
        if not items:
            raise ValidationError("select at least one source")
    elif data.get("type") == "turn":
        if (set(data) != {"type", "turn", "session_ref", "stream_ref", "reads", "gaps"}
                or type(data["turn"]) is not int or not 1 <= data["turn"] <= 1000000
                or not _digest(data["session_ref"]) or not _digest(data["stream_ref"])):
            raise ValidationError("invalid inspector turn")
        if (not isinstance(data["gaps"], list) or len(data["gaps"]) > len(GAP_CODES)
                or any(not isinstance(v, str) or v not in GAP_CODES for v in data["gaps"])):
            raise ValidationError("invalid coverage gap")
        items = data["reads"]
        event_id = f"inspector.turn.{data['turn']}"
    else:
        raise ValidationError("unknown inspector receipt")
    if not isinstance(items, list) or len(items) > MAX_FILES:
        raise ValidationError("invalid inspector item list")
    paths = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}
                or not isinstance(item["path"], str) or not valid_relative_path(item["path"])
                or item["path"] in paths or not _digest(item["sha256"])
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE_BYTES):
            raise ValidationError("invalid inspector item")
        paths.add(item["path"])
    return event_id


def validate_turn_binding(turn, evidence):
    receipts = [e["data"] for e in evidence if e.get("kind") == "inspector"]
    inventory = next((r for r in receipts if r["type"] == "inventory"), None)
    if inventory is None or any(read not in inventory["items"] for read in turn["reads"]):
        raise ValidationError("turn reads require a matching task inventory")
    if any(r.get("stream_ref") == turn["stream_ref"] for r in receipts if r["type"] == "turn"):
        raise ValidationError("this Codex event stream is already attached to a turn")
    if any(r["session_ref"] != turn["session_ref"] for r in receipts if r["type"] == "turn"):
        raise ValidationError("this task's inspector is bound to another Codex session")


def task_inspection(task):
    """Keep a frozen review and later events separate, including for imported tasks."""
    def evidence_group(events):
        return {
            "inventory": next((e["data"]["items"] for e in events
                               if e["kind"] == "inspector" and e["data"]["type"] == "inventory"), None),
            "turns": [e["data"] for e in events
                      if e["kind"] == "inspector" and e["data"]["type"] == "turn"],
            "reports": [e for e in events if e["kind"] == "context"
                        or (e["source"] == "agent-reported" and e["kind"] != "inspector")],
        }

    return {
        **{k: task.get(k) for k in ("task_id", "title", "goal", "success_criteria",
                                    "baseline_source", "baseline_missing", "context",
                                    "result", "status", "review", "resources", "revision")},
        "capture": evidence_group(task["evidence"]),
        "after_review": evidence_group(task["post_review_evidence"]),
        "coverage": "Selected inventory and matching plain cat output only. Missing receipts do not establish non-use; delivery does not establish influence.",
    }
