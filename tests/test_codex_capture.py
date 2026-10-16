"""Synthetic native-shaped Codex fixtures for privacy and event attribution."""
import os
import sqlite3
import tempfile
import unittest
import multiprocessing
import io
import json
from unittest.mock import patch

from codex_capture import collect, list_reviews, main
from review_store import (MIGRATION_1, MIGRATION_2, ConflictError, ReviewStore,
                          StaleRevisionError)
from tests.helpers import full_payload


def _capture_from_process(data_dir, session, queue):
    try:
        with ReviewStore.open(data_dir) as store:
            review_id = store.ensure_runtime_job(session, "startup")
            store.ingest_runtime_event(session, {
                "kind": "turn_start", "turn_id": "root-" + session,
                "observed_at_utc": "2026-10-16T12:00:00+00:00",
                "observed_monotonic_ns": 1, "adapter_version": "test"})
        queue.put((session, review_id, None))
    except Exception as exc:
        queue.put((session, None, type(exc).__name__))


class CodexCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="apr-native-")
        self.addCleanup(self.tmp.cleanup)
        self.data = self.tmp.name

    def hook(self, name, session, turn=None, **extra):
        payload = {"hook_event_name": name, "session_id": session,
                   "cwd": "/bait/private/project", "model": "model-test",
                   "transcript_path": "/bait/transcript.jsonl",
                   "prompt": "BAIT PROMPT MUST NEVER PERSIST",
                   "tool_name": "mcp__bait__private", "tool_input": {"secret": "BAIT INPUT"},
                   "tool_response": "BAIT OUTPUT"}
        if turn is not None:
            payload["turn_id"] = turn
        payload.update(extra)
        return payload

    def run_hook(self, name, session, turn=None, at="2026-10-16T12:00:00+00:00", mono=1, **extra):
        return collect(self.hook(name, session, turn, **extra), self.data,
                       observed_at=at, monotonic_ns=mono)

    def test_interleaved_sessions_are_private_and_resume_stable(self):
        self.run_hook("SessionStart", "native-A", source="startup")
        self.run_hook("SessionStart", "native-B", source="startup")
        a = self.run_hook("UserPromptSubmit", "native-A", "turn-A",
                          at="2026-10-16T12:00:01+00:00", mono=2)
        b = self.run_hook("UserPromptSubmit", "native-B", "turn-B",
                          at="2026-10-16T12:00:02+00:00", mono=3)
        self.run_hook("PreToolUse", "native-A", "turn-A", tool_use_id="tool-A")
        self.run_hook("PostToolUse", "native-A", "turn-A", tool_use_id="tool-A")
        self.run_hook("Stop", "native-A", "turn-A", at="2026-10-16T12:00:05+00:00", mono=5)
        self.run_hook("Interrupt", "native-B", "turn-B", at="2026-10-16T12:00:06+00:00", mono=6)
        with ReviewStore.open(self.data) as store:
            p_a = store.runtime_review_projection(a["review_id"])
            p_b = store.runtime_review_projection(b["review_id"])
            self.assertEqual(p_a["observed_turn_count"], 1)
            self.assertEqual(p_a["tool_call_count"], 1)
            self.assertIn("subagent_attribution_unverified", p_a["coverage_gaps"])
            self.assertEqual(p_a["turns"][0]["elapsed_seconds"], 4)
            self.assertEqual(p_a["turns"][0]["terminal_kind"], "turn_stopped")
            self.assertEqual(p_b["observed_turn_count"], 1)
            self.assertEqual(p_b["tool_call_count"], 0)
            self.assertEqual(p_b["turns"][0]["terminal_kind"], "turn_interrupted")
        self.run_hook("SessionStart", "native-A", source="resume")
        self.run_hook("UserPromptSubmit", "native-A", "turn-A")
        with patch("local_service.ensure_service",
                   return_value=("http://127.0.0.1:5555", "private")):
            before_rows = list_reviews(self.data)
            urls_before = {row["review_id"]: row["rating_url"] for row in before_rows}
            self.assertTrue(all("job_token" not in row for row in before_rows))
        with ReviewStore.open(self.data) as store:
            self.assertEqual(len(store.list_runtime_reviews()), 2)
            self.assertEqual(store.runtime_review_projection(a["review_id"])["observed_turn_count"], 1)
        with patch("local_service.ensure_service",
                   return_value=("http://127.0.0.1:5555", "private")):
            urls_after = {row["review_id"]: row["rating_url"]
                          for row in list_reviews(self.data)}
        self.assertEqual(urls_before, urls_after)
        self.assertEqual(len(set(urls_after.values())), 2)
        with open(os.path.join(self.data, "reviews.db"), "rb") as handle:
            db = handle.read()
        for bait in (b"BAIT PROMPT", b"BAIT INPUT", b"BAIT OUTPUT", b"/bait/private",
                     b"/bait/transcript", b"native-A", b"turn-A", b"tool-A"):
            self.assertNotIn(bait, db)

    def test_unattributed_tool_replay_and_stop_without_start_are_gaps(self):
        started = self.run_hook("SessionStart", "chat", source="startup")
        self.run_hook("PreToolUse", "chat", "root-turn", tool_use_id="early-tool")
        self.run_hook("UserPromptSubmit", "chat", "root-turn")
        self.run_hook("PreToolUse", "chat", "root-turn", tool_use_id="early-tool")
        self.run_hook("Stop", "chat", "missing-turn", at="2026-10-16T12:00:04+00:00")
        self.run_hook("PreToolUse", "chat", "root-turn", tool_use_id="no-post")
        self.run_hook("PostToolUse", "chat", None, tool_use_id="no-turn-tool")
        with ReviewStore.open(self.data) as store:
            p = store.runtime_review_projection(started["review_id"])
            self.assertEqual(p["tool_call_count"], 1)
            self.assertIn("unattributed_tool_events", p["coverage_gaps"])
            self.assertIn("missing_tool_post", p["coverage_gaps"])
            self.assertIn("terminal_without_observed_start", p["coverage_gaps"])
            self.assertNotIn("tool_post", [event["kind"] for event in store._conn.execute(
                "SELECT kind FROM runtime_events WHERE review_id=?", (started["review_id"],))])

    def test_late_start_creates_partial_binding_and_review_tools_are_excluded(self):
        first = self.run_hook("UserPromptSubmit", "late-chat", "turn-late")
        self.run_hook("PreToolUse", "late-chat", "turn-late", tool_use_id="apr-tool",
                      tool_name="mcp__agent-performance-review__request_rating")
        self.run_hook("PostToolUse", "late-chat", "turn-late", tool_use_id="apr-tool",
                      tool_name="mcp__agent_performance_review__request_rating")
        self.run_hook("PreToolUse", "late-chat", "turn-late")
        with ReviewStore.open(self.data) as store:
            p = store.runtime_review_projection(first["review_id"])
            self.assertEqual(p["coverage"], "partial")
            self.assertIn("late_start", p["coverage_gaps"])
            self.assertIn("missing_tool_identity", p["coverage_gaps"])
            self.assertEqual(p["tool_call_count"], 0)
            self.assertEqual(p["event_count"], 1)
            self.assertEqual(len(store.list_runtime_reviews()), 1)

    def test_missing_native_session_id_writes_only_sanitized_diagnostic(self):
        payload = self.hook("SessionStart", "native-id")
        payload.pop("session_id")
        self.assertIsNone(collect(payload, self.data))
        with open(os.path.join(self.data, "capture.log"), encoding="ascii") as handle:
            diagnostic = handle.read()
        self.assertIn("capture_error", diagnostic)
        for bait in ("BAIT PROMPT", "BAIT INPUT", "BAIT OUTPUT", "transcript.jsonl"):
            self.assertNotIn(bait, diagnostic)

    def test_restart_clear_and_fork_continuity_policy(self):
        start = self.run_hook("SessionStart", "same-id", source="startup")
        with ReviewStore.open(self.data) as store:
            bound = store.ensure_runtime_job("same-id", "resume")
            self.assertEqual(bound, start["review_id"])
            with self.assertRaises(ConflictError):
                store.ensure_runtime_job("same-id", "clear")
            with self.assertRaises(ConflictError):
                store.ensure_runtime_job("same-id", "fork")
        with ReviewStore.open(self.data) as store:
            self.assertEqual(len(store.list_runtime_reviews()), 1)

    def test_resume_lifecycle_marker_dedupes_and_exposes_occurrence_gap(self):
        started = self.run_hook("SessionStart", "resume-chat", source="startup",
                                model="startup-model")
        self.run_hook("SessionStart", "resume-chat", source="resume",
                      model="first-resume-model")
        self.run_hook("SessionStart", "resume-chat", source="resume",
                      model="changed-resume-model")
        with ReviewStore.open(self.data) as store:
            projection = store.runtime_review_projection(started["review_id"])
            self.assertEqual(projection["event_count"], 2)
            self.assertEqual(projection["models"], [])
            self.assertIn("lifecycle_occurrence_unverifiable",
                          projection["coverage_gaps"])
        self.run_hook("UserPromptSubmit", "resume-chat", "resume-turn",
                      model="turn-model")
        with ReviewStore.open(self.data) as store:
            projection = store.runtime_review_projection(started["review_id"])
            self.assertEqual(projection["models"], ["turn-model"])
            self.assertEqual(projection["event_count"], 3)

    def test_clear_reuse_records_safe_gap_and_hook_stdout_stays_empty(self):
        first = self.run_hook("SessionStart", "reuse", source="startup")
        payload = self.hook("SessionStart", "reuse", source="clear")
        raw = json.dumps(payload)
        with patch("sys.stdin", io.StringIO(raw)), patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(main(["--data-dir", self.data, "capture"]), 0)
            self.assertEqual(out.getvalue(), "")
        with ReviewStore.open(self.data) as store:
            evidence = store.runtime_review_projection(first["review_id"])
            self.assertIn("identity_continuity_unsupported", evidence["coverage_gaps"])
        later = self.hook("UserPromptSubmit", "reuse", "post-clear-turn")
        with patch("sys.stdin", io.StringIO(json.dumps(later))), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--data-dir", self.data, "capture"]), 0)
        with ReviewStore.open(self.data) as store:
            self.assertEqual(store.runtime_review_projection(first["review_id"])["observed_turn_count"], 0)

    def test_stale_card_rejected_and_saved_runtime_watermark_is_frozen(self):
        start = self.run_hook("SessionStart", "rating-chat", source="startup")
        old_revision = None
        with ReviewStore.open(self.data) as store:
            job = store._conn.execute("SELECT revision FROM jobs WHERE review_id=?",
                                      (start["review_id"],)).fetchone()
            old_revision = job[0]
        self.run_hook("UserPromptSubmit", "rating-chat", "turn-1")
        stale = full_payload(review_id=start["review_id"])
        with ReviewStore.open(self.data) as store:
            with self.assertRaises(StaleRevisionError):
                store.save_rating(stale, expected_revision=old_revision)
            current = store._conn.execute("SELECT revision FROM jobs WHERE review_id=?",
                                          (start["review_id"],)).fetchone()[0]
            receipt = store.save_rating(stale, expected_revision=current)
            self.assertEqual(receipt["status"], "recorded")
            frozen = store.runtime_rating_projection(start["review_id"])
            frozen_count = frozen["event_count"]
        self.run_hook("UserPromptSubmit", "rating-chat", "turn-2",
                      at="2020-01-01T12:00:00+00:00")
        with ReviewStore.open(self.data) as store:
            self.assertEqual(store.runtime_rating_projection(start["review_id"])["event_count"],
                             frozen_count)
            live = store.runtime_review_projection(start["review_id"])
            self.assertEqual(live["observed_turn_count"], 2)
            self.assertIsNotNone(live["evidence_after_rating"])
            self.assertEqual(live["post_rating_event_count"], 1)
            self.assertEqual(live["post_rating_gap_count"], 0)

    def test_post_rating_gap_is_labeled_and_frozen_gap_watermark_stays_unchanged(self):
        started = self.run_hook("SessionStart", "rated-gap-chat", source="startup")
        with ReviewStore.open(self.data) as store:
            revision = store._conn.execute(
                "SELECT revision FROM jobs WHERE review_id=?",
                (started["review_id"],)).fetchone()[0]
            receipt = store.save_rating(full_payload(review_id=started["review_id"]),
                                        expected_revision=revision)
            self.assertEqual(receipt["status"], "recorded")
            frozen = store.runtime_rating_projection(started["review_id"])
            frozen_gap_watermark = frozen["projection"]["gap_watermark"]
        with ReviewStore.open(self.data) as store:
            self.assertTrue(store.mark_runtime_gap("rated-gap-chat", "missing_tool_identity"))
            live = store.runtime_review_projection(started["review_id"])
            frozen_after = store.runtime_rating_projection(started["review_id"])
        self.assertEqual(frozen_after["projection"]["gap_watermark"], frozen_gap_watermark)
        self.assertEqual(live["post_rating_event_count"], 0)
        self.assertEqual(live["post_rating_gap_count"], 1)
        self.assertEqual(live["post_rating_gap_keys"], ["missing_tool_identity"])
        self.assertIsNotNone(live["evidence_after_rating"])

    def test_runtime_gap_invalidates_pending_card_revision(self):
        started = self.run_hook("SessionStart", "gap-chat", source="startup")
        with ReviewStore.open(self.data) as store:
            old_revision = store._conn.execute(
                "SELECT revision FROM jobs WHERE review_id=?",
                (started["review_id"],)).fetchone()[0]
            self.assertTrue(store.mark_runtime_gap("gap-chat", "missing_tool_identity"))
            current_revision = store._conn.execute(
                "SELECT revision FROM jobs WHERE review_id=?",
                (started["review_id"],)).fetchone()[0]
            self.assertEqual(current_revision, old_revision + 1)
            with self.assertRaises(StaleRevisionError):
                store.save_rating(full_payload(review_id=started["review_id"]),
                                  expected_revision=old_revision)
            projection = store.runtime_review_projection(started["review_id"])
            self.assertIn("missing_tool_identity", projection["coverage_gaps"])
            self.assertEqual(projection["post_rating_gap_count"], 0)
            self.assertEqual(projection["post_rating_gap_keys"], [])
            self.assertIsNone(projection["evidence_after_rating"])

    def test_existing_v2_store_upgrades_without_changing_manual_job(self):
        db_path = os.path.join(self.data, "reviews.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(MIGRATION_1)
            conn.executescript(MIGRATION_2)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,'old')")
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(2,'old')")
            conn.execute("INSERT INTO jobs(review_id,job_token,snapshot,revision,status,updated_at)"
                         " VALUES('manual-id','manual-token','{}',7,'pending','old')")
            conn.commit()
        finally:
            conn.close()
        with ReviewStore.open(self.data) as store:
            self.assertEqual(store.migration_versions(), [1, 2, 3])
            card = store.get_card("manual-token")
            self.assertEqual(card["revision"], 7)
            self.assertEqual(card["status"], "pending")

    def test_concurrent_processes_share_private_salt_and_write_events(self):
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        sessions = ["parallel-one", "parallel-two"]
        processes = [ctx.Process(target=_capture_from_process,
                                 args=(self.data, session, queue)) for session in sessions]
        for process in processes: process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(error is None for _, _, error in results), results)
        self.assertEqual(len({review_id for _, review_id, _ in results}), 2)
        with ReviewStore.open(self.data) as store:
            self.assertEqual(len(store.list_runtime_reviews()), 2)


if __name__ == "__main__":
    unittest.main()
