"""Boundary tests for the opt-in Codex JSONL observer. All files are synthetic."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from codex_context import (MAX_FILE_BYTES, MAX_LINE_BYTES, observe_turn, snapshot)
from review_store import ReviewStore
from task_schema import ValidationError
from tests.test_tasks import task_payload


def lines(*events):
    return b"".join((json.dumps(e) + "\n").encode() for e in events)


THREAD = {"type": "thread.started", "thread_id": "synthetic-thread"}
START = {"type": "turn.started"}
END = {"type": "turn.completed"}
BODY = "operator fixture instructions \u2603.\n"


def item(command, **overrides):
    return {"type": "item.completed",
            "item": {"type": "command_execution", "command": command,
                     "exit_code": 0, "aggregated_output": BODY,
                     "status": "completed", **overrides}}


class ObserverTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name).resolve()
        self.root = self.work / "Source Files"
        self.root.mkdir()
        self.path = self.root / "rules.md"
        self.path.write_text(BODY, encoding="utf-8")
        self.cat = "cat -- " + shlex.quote(str(self.path))
        self.inventory = snapshot(self.root, ["rules.md"])

    def observe(self, data):
        return observe_turn(io.BytesIO(lines(THREAD) + data), 1, self.root, self.inventory, "task1")

    def test_snapshot_and_hash_match_shell_quoted_command(self):
        data = BODY.encode()
        metadata = {"path": "rules.md", "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
        self.assertEqual(self.inventory["items"], [metadata])
        self.assertNotIn(str(self.root), json.dumps(self.inventory))
        native = item("/bin/zsh -lc " + shlex.quote(self.cat))
        relative = item("cat ./rules.md", cwd=str(self.root))
        stream = lines(START, native, relative, END)
        result = self.observe(stream)
        expected_digest = hashlib.sha256(b"apr.codex-inspector.stream.v1\0task1\0" +
                                         lines(THREAD) + stream).hexdigest()
        self.assertEqual(result, {"type": "turn", "turn": 1, "gaps": [], "reads": [metadata],
                                  "session_ref": hashlib.sha256(b"task1:synthetic-thread").hexdigest(),
                                  "stream_ref": expected_digest})

    def test_reject_unsupported_inaccessible_and_sensitive_paths(self):
        (self.root / "escape").symlink_to(self.work, target_is_directory=True)
        (self.root / "link.txt").symlink_to(self.path)
        (self.root / "binary").write_bytes(b"abc\x00")
        (self.root / "invalidutf8").write_bytes(b"\x80")
        (self.root / "large").write_bytes(b"A" * (MAX_FILE_BYTES + 1))
        (self.root / "keys.txt").write_text("ghp_123456789012")
        (self.root / "config.txt").write_text('{"password": "sensitive"}')
        os.mkfifo(self.root / "pipe")
        for name in (".env", "nested/../rules.md", str(self.path), "escape/path", "link.txt",
                     "keys.txt", "config.txt", "*", "binary", "invalidutf8", "large",
                     "pipe", "my-credentials.txt", ".git/config"):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                snapshot(self.root, [name])
        for selection in ([], ["rules.md", "rules.md"]):
            with self.assertRaises(ValidationError):
                snapshot(self.root, selection)

    def test_public_doc_path_and_literal_are_eligible_but_api_key_is_rejected(self):
        name = "task-recording-standard.md"
        content = ("See " + name + " for guidance.").encode()
        (self.root / name).write_bytes(content)
        self.assertEqual(snapshot(self.root, [name])["items"], [
            {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}])
        (self.root / name).write_text("sk-proj-testexample12345678")
        with self.assertRaises(ValidationError):
            snapshot(self.root, [name])
        with self.assertRaises(ValidationError):
            snapshot(self.root, ["sk-testexample12345678.md"])

    def test_stream_fingerprint_includes_discarded_bytes_and_is_task_specific(self):
        prefix = lines(THREAD, START) + b"x" * (MAX_LINE_BYTES + 17)
        def receipt(suffix, task_id):
            stream = io.BytesIO(prefix + suffix + b"\n" + lines(END))
            return observe_turn(stream, 1, self.root, self.inventory, task_id)
        first = receipt(b"A", "task1")
        self.assertEqual(first["gaps"], ["oversize_event"])
        self.assertNotEqual(first["stream_ref"], receipt(b"B", "task1")["stream_ref"])
        self.assertNotEqual(first["stream_ref"], receipt(b"A", "task2")["stream_ref"])

    def test_no_claim_for_ambiguous_failed_unselected_or_modified_read(self):
        for cmd, overrides, gap in (
                ("cat rules.md", {}, "missing_cwd"),
                (self.cat, {"aggregated_output": BODY.rstrip()}, "content_mismatch"),
                (self.cat, {"aggregated_output": None}, "content_mismatch"),
                (self.cat, {"exit_code": False}, "command_failed"),
                (self.cat, {"status": "failed"}, "command_failed"),
                ("cat /unselected/rules.md", {}, "unselected_path"),
                ("cat rules.md", {"cwd": "/somewhere/else"}, "unselected_path"),
                (self.cat + " | tail", {}, "unsupported_command"),
                (self.cat + "; printf secret", {}, "unsupported_command"),
                ("cat 'rules.md' 'second.md'", {}, "unsupported_command"),
                ("cat $(choose)", {}, "unsupported_command"),
                ("cat *.md", {}, "unsupported_command")):
            with self.subTest(cmd=cmd, gap=gap):
                result = self.observe(lines(START, item(cmd, **overrides), END))
                self.assertEqual(result["reads"], [])
                self.assertEqual(result["gaps"], [gap])

    def test_failed_truncated_malformed_and_unknown_events_are_sanitized(self):
        raw = (lines(START) + b'not-json-with-private-message\n'
               + b'x' * (MAX_LINE_BYTES + 1) + b'\n'
               + lines({"type": "item.completed", "item": {"type": "mcp_tool_call",
                                                            "result": BODY}},
                       {"type": "item.completed", "item": {"type": "agent_message",
                                                            "text": BODY}},
                       item(self.cat)))
        res = self.observe(raw)
        self.assertEqual(res["reads"], self.inventory["items"])
        self.assertEqual(set(res["gaps"]), {"incomplete_stream", "oversize_event",
                                            "invalid_event", "unsupported_tool"})
        self.assertNotIn(BODY.strip(), json.dumps(res))
        self.assertEqual(self.observe(lines(START, {"type": "turn.failed", "error": BODY}))["gaps"],
                         ["turn_failed"])
        with self.assertRaises(ValidationError):
            self.observe(lines(item(self.cat), END))
        with self.assertRaises(ValidationError):
            self.observe(b"")

    def test_reject_cross_turn_or_cross_root_attribution(self):
        for raw in (lines(START, END, START), lines(START, START),
                    lines({"type": "thread.started"}, START, END, {"type": "thread.started"})):
            with self.assertRaises(ValidationError):
                self.observe(raw)
        with self.assertRaises(ValidationError):
            observe_turn(io.BytesIO(), 1, self.work, self.inventory, "task1")
        with self.assertRaises(ValidationError):
            observe_turn(io.BytesIO(), 0, self.root, self.inventory, "task1")

    def test_require_identity_and_report_unfinished_command(self):
        for bad in (lines(START, END), lines({"type": "thread.started", "thread_id": ""}, START)):
            with self.assertRaises(ValidationError):
                observe_turn(io.BytesIO(bad), 1, self.root, self.inventory, "task1")
        event = {"type": "item.started", "item": {"type": "command_execution", "id": "started-id"}}
        self.assertEqual(self.observe(lines(START, event, END))["gaps"], ["incomplete_stream"])
        self.assertEqual(self.observe(lines(START, event, item(self.cat, id="started-id"), END))
                         ["gaps"], [])

    def cli(self, command, task_id, *args, env=None, stdin=b""):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "codex_context.py"),
             command, "--root", str(self.root), "--task-id", task_id, *args],
            input=stdin, env=env, capture_output=True, timeout=20)

    def test_cli_requires_both_opt_ins_before_opening_store(self):
        dest = self.work / "unused"
        env = dict(os.environ, APR_DATA_DIR=str(dest))
        env.pop("APR_CONTEXT_CAPTURE", None)
        result = self.cli("inventory", "missing", "rules.md", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(dest.exists())
        env.pop("APR_DATA_DIR")
        env["APR_CONTEXT_CAPTURE"] = "1"
        result = self.cli("turn", "missing", "--turn", "1", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"APR_DATA_DIR", result.stderr)

    def test_session_hint_for_explicit_resume_is_separate_from_receipt(self):
        native = "01a10000-0000-7000-8000-000000000001"
        raw = lines({"type": "thread.started", "thread_id": native}, START, END)
        hints = {}
        turn = observe_turn(io.BytesIO(raw), 1, self.root, self.inventory, "task1", resume_hint=hints)
        self.assertEqual(hints, {"resume_id": native})
        self.assertNotIn(native, json.dumps(turn))

    def test_cli_persists_only_metadata_and_idempotent_turn(self):
        dest = self.work / "db"
        with ReviewStore.open(dest) as store:
            task = store.tasks.start_task(task_payload())["task_id"]
        env = dict(os.environ, APR_CONTEXT_CAPTURE="1", APR_DATA_DIR=str(dest))
        doc = "task-recording-standard.md"
        (self.root / doc).write_text("Public guidance from " + doc)
        inv = self.cli("inventory", task, "rules.md", doc, env=env)
        self.assertEqual(inv.returncode, 0, inv.stderr)
        stream = lines({"type": "thread.started", "thread_id": "PRIVATE_THREAD_ID"}, START,
                       item(self.cat), item("printf PRIVATE_COMMAND", aggregated_output="PRIVATE_OUTPUT"),
                       END)
        for expected in ("recorded", "already_recorded"):
            result = self.cli("turn", task, "--turn", "1", env=env, stdin=stream)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], expected)
        renumbered = self.cli("turn", task, "--turn", "2", env=env, stdin=stream)
        self.assertNotEqual(renumbered.returncode, 0)
        different = lines({"type": "thread.started", "thread_id": "PRIVATE_THREAD_ID"}, START, END)
        conflict = self.cli("turn", task, "--turn", "1", env=env, stdin=different)
        self.assertNotEqual(conflict.returncode, 0)
        with ReviewStore.open(dest) as store:
            events = store.tasks.get_task(task)["evidence"]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]["data"]["reads"], self.inventory["items"])
        for file in dest.iterdir():
            if file.is_file():
                content = file.read_bytes()
                for token in (str(self.root), BODY.strip(), "PRIVATE_COMMAND", "PRIVATE_OUTPUT",
                              "PRIVATE_THREAD_ID"):
                    self.assertNotIn(token.encode(), content)


if __name__ == "__main__":
    unittest.main()
