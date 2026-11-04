"""Opt-in local context inspector for one operator-selected task and Codex exec turn."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from task_schema import SECRET_VALUE, ValidationError, is_credential_key

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
GAP_CODES = frozenset({"unsupported_tool", "unsupported_command", "unselected_path",
                       "missing_cwd", "command_failed", "content_mismatch", "invalid_event",
                       "oversize_event", "incomplete_stream", "turn_failed"})
SAFE_COMPONENT = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9._ -]*")
SENSITIVE_NAME = re.compile(
    r"(?:^|[._ -])(?:env|secret[s]?|credential[s]?|password[s]?|passwd|token[s]?|api[-_ ]?key|"
    r"private[-_ ]?key|keychain|id_(?:rsa|ed25519|ecdsa|dsa)|netrc|npmrc|kubeconfig|"
    r"pem|p12|pfx|key|kdbx)(?:$|[._ -])", re.I)
# The shared task validator intentionally rejects even embedded sk- strings.
# In ordinary document names "task-recording-standard.md" contains that suffix.
# Only treat sk- as a credential prefix at a token boundary for local snapshots.
FILE_SECRET_VALUE = re.compile(r"(?!(?<=[A-Za-z0-9])sk-)(?:" + SECRET_VALUE.pattern + ")",
                               SECRET_VALUE.flags)
SECRET_ASSIGNMENT = re.compile(
    r"""(?im)\b(?:password|passwd|api[_-]?key|secret|token|access[_-]?key)\s*["']?\s*[:=]\s*\S""")
# Spaces require shell quotes or escapes; shell operators and expansion are intentionally absent.
PLAIN_CAT = re.compile(
    r"""(?:/bin/|/usr/bin/)?cat[ \t]+(?:--[ \t]+)?(?:[A-Za-z0-9_./-]+|'[A-Za-z0-9_./ -]+'|"[A-Za-z0-9_./ -]+"|(?:[A-Za-z0-9_./-]|\\ )+)""")
SHELLS = {"/bin/sh", "/bin/bash", "/bin/zsh", "/usr/bin/bash", "/usr/bin/zsh"}


def valid_relative_path(path):
    if not isinstance(path, str) or len(path) > 512 or FILE_SECRET_VALUE.search(path):
        return False
    return all(SAFE_COMPONENT.fullmatch(part) and part == part.strip()
               and not SENSITIVE_NAME.search(part) and not is_credential_key(part)
               for part in path.split("/"))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read_selected(root_fd, path):
    # Walk from the already-open root directory. Refuse symlinks at every hop,
    # even when a directory is swapped between validation and open.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.dup(root_fd)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            next_dir = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = next_dir
        fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValidationError("select regular text files within the size limit")
            data = handle.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(directory)
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError("source file exceeds size limit")
    content = data.decode("utf-8")
    if (any(ord(char) < 32 and char not in "\t\n\r" for char in content)
            or FILE_SECRET_VALUE.search(content) or SECRET_ASSIGNMENT.search(content)):
        raise ValidationError("binary or credential-shaped material is ineligible")
    return {"path": path, "sha256": _sha256(data), "bytes": len(data)}


def snapshot(root, paths):
    if (not 1 <= len(paths) <= MAX_FILES or len(set(paths)) != len(paths)
            or not all(valid_relative_path(path) for path in paths)):
        raise ValidationError("select unique, eligible relative file paths")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        items = [_read_selected(root_fd, path) for path in sorted(paths)]
    finally:
        os.close(root_fd)
    return {"type": "inventory", "root_id": _sha256(os.fsencode(root)), "items": items}


def _cat_operand(command):
    if not isinstance(command, str) or len(command) > 4096 or "\n" in command or "\r" in command:
        return None
    try:
        words = shlex.split(command)
        if len(words) == 3 and words[0] in SHELLS and words[1] in {"-lc", "-c"}:
            command = words[2]
        if not PLAIN_CAT.fullmatch(command):
            return None
        operand = shlex.split(command)[-1]
        return operand if not operand.startswith("-") else None
    except ValueError:
        return None


def _verify_read(item, root, items, reads, gaps):
    path = _cat_operand(item.get("command"))
    if path is None:
        gaps.add("unsupported_command")
        return
    if (item.get("status") != "completed" or type(item.get("exit_code")) is not int
            or item["exit_code"] != 0):
        gaps.add("command_failed")
        return
    if not os.path.isabs(path):
        cwd = item.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            gaps.add("missing_cwd")
            return
        path = os.path.join(cwd, path)
    absolute = Path(path)
    relative = str(absolute.relative_to(root)) if root in absolute.parents else ""
    metadata = items.get(relative)
    if metadata is None:
        gaps.add("unselected_path")
        return
    output = item.get("aggregated_output")
    data = output.encode("utf-8", errors="surrogatepass") if isinstance(output, str) else None
    if data is None or len(data) != metadata["bytes"] or _sha256(data) != metadata["sha256"]:
        gaps.add("content_mismatch")
        return
    reads[relative] = metadata


def observe_turn(stream, turn, root, inventory, task_id, *, resume_hint=None):
    """Read bounded binary JSONL from one trusted Codex invocation, discarding raw events."""
    if type(turn) is not int or turn < 1:
        raise ValidationError("turn must be a positive integer")
    if inventory["root_id"] != _sha256(os.fsencode(root)):
        raise ValidationError("source root does not match the inventory")
    items = {item["path"]: item for item in inventory["items"]}
    reads, gaps = {}, set()
    started = ended = False
    session_ref = None
    pending = set()
    stream_hash = hashlib.sha256(b"apr.codex-inspector.stream.v1\0" + task_id.encode() + b"\0")
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            break
        stream_hash.update(line)
        if len(line) > MAX_LINE_BYTES:
            gaps.add("oversize_event")
            while line and not line.endswith(b"\n"):
                line = stream.readline(MAX_LINE_BYTES + 1)
                stream_hash.update(line)
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            gaps.add("invalid_event")
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            gaps.add("invalid_event")
            continue
        kind = event["type"]
        if kind == "thread.started":
            if session_ref or started or ended:
                raise ValidationError("provide a single Codex turn")
            thread = event.get("thread_id")
            if not isinstance(thread, str) or not thread.strip() or len(thread) > 256:
                raise ValidationError("missing Codex thread identity")
            session_ref = _sha256((task_id + ":" + thread).encode())
            # Emit a resume hint only on explicit request, after a successful commit.
            # This native handle is never part of the stored receipt.
            if resume_hint is not None and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", thread):
                resume_hint["resume_id"] = thread
        elif kind == "turn.started":
            if started or ended:
                raise ValidationError("provide a single Codex turn")
            started = True
        elif kind in {"turn.completed", "turn.failed"}:
            if ended:
                raise ValidationError("provide a single Codex turn")
            ended = True
            if kind == "turn.failed":
                gaps.add("turn_failed")
        elif kind == "item.started" and started and not ended:
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "command_execution":
                item_id = item.get("id")
                if isinstance(item_id, str) and len(item_id) <= 256 and len(pending) < 256:
                    pending.add(item_id)
                else:
                    gaps.add("incomplete_stream")
        elif kind == "item.completed" and started and not ended:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                gaps.add("invalid_event")
            elif item["type"] == "command_execution":
                if isinstance(item.get("id"), str):
                    pending.discard(item["id"])
                _verify_read(item, root, items, reads, gaps)
            elif item["type"] not in {"agent_message", "reasoning", "todo_list", "user_message"}:
                gaps.add("unsupported_tool")
        elif kind == "error":
            gaps.add("incomplete_stream")
        elif kind not in {"item.started", "item.updated"}:
            gaps.add("invalid_event")
    if session_ref is None:
        raise ValidationError("provide one identified Codex thread")
    if not started:
        raise ValidationError("no Codex turn started")
    if not ended or pending:
        gaps.add("incomplete_stream")
    return {"type": "turn", "turn": turn, "session_ref": session_ref, "stream_ref": stream_hash.hexdigest(),
            "reads": [reads[p] for p in sorted(reads)],
            "gaps": sorted(gaps)}


def main(argv=None):
    from review_store import ReviewStore, StoreError

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory_cmd = commands.add_parser("inventory", help="snapshot explicit relative paths once")
    turn_cmd = commands.add_parser("turn", help="consume one turn of codex exec --json on stdin")
    for cmd in (inventory_cmd, turn_cmd):
        cmd.add_argument("--root", type=Path, required=True)
        cmd.add_argument("--task-id", required=True)
    inventory_cmd.add_argument("paths", nargs="+")
    turn_cmd.add_argument("--turn", type=int, required=True)
    turn_cmd.add_argument("--show-resume-id", action="store_true",
                          help="print the native Codex session UUID for an explicit resume (not stored)")
    args = parser.parse_args(argv)
    hints = {}
    data_dir = os.environ.get("APR_DATA_DIR", "").strip()
    if os.environ.get("APR_CONTEXT_CAPTURE") != "1" or not data_dir:
        print("Set APR_DATA_DIR and APR_CONTEXT_CAPTURE=1 to enable capture.", file=sys.stderr)
        return 2
    try:
        root = args.root.resolve(strict=True)
        if not (Path(data_dir) / "reviews.db").is_file():
            raise ValidationError("select an existing APR task database")
        with ReviewStore.open(data_dir) as store:
            task = store.tasks.get_task(args.task_id)
            if not task:
                raise ValidationError("select an existing APR task")
            if args.command == "inventory":
                data = snapshot(root, args.paths)
                event_id = "inspector.inventory"
            else:
                events = task["evidence"] + task.get("post_review_evidence", [])
                inventory = next((e["data"] for e in events if e["kind"] == "inspector"
                                  and e["event_id"] == "inspector.inventory"), None)
                if inventory is None:
                    raise ValidationError("record an inventory first")
                data = observe_turn(sys.stdin.buffer, args.turn, root, inventory, args.task_id,
                                    resume_hint=hints if args.show_resume_id else None)
                event_id = "inspector.turn." + str(args.turn)
            result = store.tasks.append_inspector(args.task_id, event_id, data)
        print(json.dumps({"event_id": event_id, "status": result["status"], **hints}))
    except (OSError, ValueError, StoreError, ValidationError, RuntimeError):
        # Exception text may contain absolute paths, credentials, or raw input.
        print("Capture rejected. Check task, root, file selection, turn and existing events.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
