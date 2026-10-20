"""Task lifecycle, idempotency, privacy, and legacy migration contracts."""
import json
import unittest
import math

from review_store import ReviewStore
from task_schema import ValidationError
from tests.helpers import CARD, fresh_data_dir, full_payload


def task_payload(**overrides):
    value = {
        "request_id": "req-1", "title": "Implement feature",
        "category": "coding", "goal": "Add task lifecycle",
        "success_criteria": ["A task can be reviewed"],
        "runtime": "Codex", "model": "model-x",
        "harness": {"mode": "existing", "name": "APR", "version": "1",
                    "roles": ["agent"], "loops": ["edit"],
                    "rules": ["preserve evidence"], "tools": ["shell"],
                    "context_categories": ["repo"], "definitions": {}},
        "parent_task_id": None, "context": {"user-stated": ["ship feature"]},
        "baseline_missing": False,
    }
    value.update(overrides)
    return value


class TaskLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.store = ReviewStore.open(fresh_data_dir(self))
        self.tasks = self.store.tasks

    def tearDown(self):
        self.store.close()

    def test_task_lifecycle_preserves_missing_ratings_and_requires_human_review(self):
        task = self.tasks.start_task(task_payload())
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["baseline_source"], "agent-reported")
        self.tasks.append_evidence(task["task_id"], "event-1", "milestone",
                                   "agent-reported", {"text": "implemented"})
        finished = self.tasks.finish_task(task["task_id"], "event-2", "Done",
                                          verification={"passed": True})
        self.assertEqual(finished["status"], "awaiting_review")
        self.assertIsNone(finished["review"])
        reviewed = self.tasks.save_review(task["task_id"], {
            "completion": "yes", "ratings": {"overall": 4},
            "gap_reasons": [], "revision": finished["revision"]})
        self.assertEqual(reviewed["review"]["ratings"], {"overall": 4})
        self.assertEqual(reviewed["status"], "reviewed")

    def test_idempotent_event_rejects_payload_changes_and_does_not_duplicate(self):
        task = self.tasks.start_task(task_payload())
        self.tasks.append_evidence(task["task_id"], "same-id", "resource",
                                   "runtime-observed", {"tool_calls": {"value": 2, "unit": "calls"}})
        self.tasks.append_evidence(task["task_id"], "same-id", "resource",
                                   "runtime-observed", {"tool_calls": {"value": 2, "unit": "calls"}})
        with self.assertRaises(Exception):
            self.tasks.append_evidence(task["task_id"], "same-id", "resource",
                                       "runtime-observed", {"tool_calls": {"value": 3, "unit": "calls"}})
        self.assertEqual(len(self.tasks.get_task(task["task_id"])["evidence"]), 1)

    def test_finish_replay_is_exact_and_cannot_rewind_a_later_finish(self):
        task = self.tasks.start_task(task_payload())
        interrupted = self.tasks.finish_task(task["task_id"], "finish-old", "Partial",
                                             interrupted=True)
        final = self.tasks.finish_task(task["task_id"], "finish-final", "Complete")
        replay = self.tasks.finish_task(task["task_id"], "finish-old", "Partial",
                                        interrupted=True)
        self.assertEqual(replay["status"], "awaiting_review")
        self.assertEqual(replay["result"], "Complete")
        self.assertEqual(replay["revision"], final["revision"])
        with self.assertRaises(ValidationError):
            self.tasks.append_evidence(task["task_id"], "finish-final", "context",
                                       "agent-reported", {"result": "Complete",
                                                           "verification": None,
                                                           "interrupted": False})
        self.assertEqual(interrupted["status"], "interrupted")

    def test_resource_provenance_and_metric_validation(self):
        task = self.tasks.start_task(task_payload())
        with self.assertRaises(ValidationError):
            self.tasks.append_evidence(task["task_id"], "resource-shadow", "resource",
                                       "runtime-observed", {"tool_calls": {"value": 2, "unit": "calls"},
                                                             "source": "imported"})
        self.tasks.append_evidence(task["task_id"], "resource-1", "resource",
                                   "runtime-observed", {"tool_calls": {"value": 2, "unit": "calls"},
                                                         "elapsed": {"value": 0.8, "unit": "s"}})
        stored = self.tasks.get_task(task["task_id"])
        self.assertEqual(stored["resources"]["resource-1"]["source"], "runtime-observed")
        for bad in ({"calls": {"value": -1, "unit": "calls"}},
                    {"calls": {"value": math.inf, "unit": "calls"}},
                    {"calls": {"value": True, "unit": "calls"}},
                    {"calls": {"value": 2, "unit": ""}}, {"calls": 2}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.tasks.append_evidence(task["task_id"], "bad-" + str(len(str(bad))),
                                           "resource", "agent-reported", bad)

    def test_finish_and_append_reject_identical_cross_operation_ids_after_restart(self):
        data = {"result": "Done", "verification": None, "interrupted": False}
        for first in ("append", "finish"):
            with self.subTest(first=first):
                task = self.tasks.start_task(task_payload(request_id="collision-" + first))
                task_id = task["task_id"]
                if first == "append":
                    self.tasks.append_evidence(task_id, "finish", "milestone", "agent-reported", data)
                else:
                    self.tasks.finish_task(task_id, "finish", "Done")
                data_dir = self.store.data_dir
                self.store.close()
                self.store = ReviewStore.open(data_dir)
                self.tasks = self.store.tasks
                before = self.tasks.get_task(task_id)
                with self.assertRaises(ValidationError):
                    if first == "append":
                        self.tasks.finish_task(task_id, "finish", "Done")
                    else:
                        self.tasks.append_evidence(task_id, "finish", "milestone", "agent-reported", data)
                self.assertEqual(self.tasks.get_task(task_id), before)

    def test_finish_retries_preserve_frozen_review_and_post_review_evidence(self):
        task = self.tasks.start_task(task_payload(request_id="finish-retry-review"))
        task_id = task["task_id"]
        self.tasks.finish_task(task_id, "old", "Partial", interrupted=True)
        finished = self.tasks.finish_task(task_id, "final", "Done")
        reviewed = self.tasks.save_review(task_id, {
            "completion": "yes", "ratings": {"overall": 4}, "gap_reasons": [],
            "revision": finished["revision"]})
        self.tasks.finish_task(task_id, "later", "Follow-up")
        before = self.tasks.get_task(task_id)
        for event_id, result, interrupted in (("old", "Partial", True),
                                               ("final", "Done", False),
                                               ("later", "Follow-up", False)):
            self.assertEqual(self.tasks.finish_task(task_id, event_id, result,
                                                   interrupted=interrupted), before)
        self.assertEqual(before["review"], reviewed["review"])
        self.assertEqual(before["evidence"], reviewed["evidence"])
        self.assertEqual(before["revision"], reviewed["revision"])
        self.assertEqual(before["result"], "Done")
        with self.assertRaises(ValidationError):
            self.tasks.append_evidence(task_id, "later", "milestone", "agent-reported",
                {"result": "Follow-up", "verification": None, "interrupted": False})

    def test_legacy_append_finish_collision_is_rejected_without_modifying_record(self):
        task = self.tasks.start_task(task_payload(request_id="legacy-collision"))
        task_id = task["task_id"]
        event = {"event_id": "finish", "kind": "milestone", "source": "agent-reported",
                 "data": {"result": "Done", "verification": None, "interrupted": False}}
        # Synthetic pre-discriminator row, not real user data.
        self.tasks.db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?)",
            (task_id, "finish", "milestone", "agent-reported",
             json.dumps(event, sort_keys=True, separators=(",", ":")), task["created_at"]))
        stored = self.tasks._load(task_id)
        stored["evidence"].append({**event, "created_at": task["created_at"]})
        stored["revision"] += 1
        self.tasks._save(stored)
        before = self.tasks.get_task(task_id)
        with self.assertRaises(ValidationError):
            self.tasks.finish_task(task_id, "finish", "Done")
        self.assertEqual(self.tasks.get_task(task_id), before)
        self.assertEqual(self.tasks.append_evidence(task_id, "finish", "milestone",
            "agent-reported", event["data"])["status"], "already_recorded")

    def test_share_withholds_file_uris_and_punctuation_delimited_local_paths(self):
        private_values = [
            "file:///Users/alice/private/repo/report.txt",
            "file:/tmp/private/report.txt",
            "https://example.test/redirect?target=file:///Users/alice/report.txt",
            "(file:///C:/Users/alice/private/report.txt)",
            "report=(/Users/alice/private/report.txt)",
            'report="/home/alice/private/report.txt"',
            "report:/private/tmp/report.txt",
            "[/Volumes/private/report.txt]",
            r"report=(C:\Users\alice\private\report.txt)",
            "report=C:/Users/alice/private/report.txt",
            r"report=\\server\private\report.txt",
        ]
        task = self.tasks.start_task(task_payload(request_id="private-paths"))
        for index, value in enumerate(private_values):
            self.tasks.append_evidence(task["task_id"], "artifact-" + str(index),
                "artifact", "agent-reported", {"uri": value})
        safe = {"uri": "https://example.test/Users/alice/report.txt",
                "note": "Ordinary text about / resources and C: drive",
                "link": "https://example.test/files?q=/home/example"}
        self.tasks.append_evidence(task["task_id"], "safe", "artifact", "agent-reported", safe)
        finished = self.tasks.finish_task(task["task_id"], "finish", "Done")
        self.tasks.save_review(task["task_id"], {"completion": "yes",
            "ratings": {"overall": 4}, "gap_reasons": [], "revision": finished["revision"]})
        preview = self.tasks.prepare_share(task["task_id"], ["artifacts"])
        artifacts = preview["payload"]["details"]["artifacts"]
        for artifact, value in zip(artifacts, private_values):
            with self.subTest(value=value):
                self.assertEqual(artifact["data"]["uri"], "[local path withheld]")
                self.assertNotIn(value, json.dumps(preview["payload"]))
        self.assertEqual(artifacts[-1]["data"], safe)
        self.assertEqual(self.tasks.get_task(task["task_id"])["evidence"][0]["data"]["uri"],
                         private_values[0])
        self.assertEqual(self.tasks.get_share_preview(preview["preview_id"])["payload"],
                         preview["payload"])

    def test_community_secret_values_rejected_on_new_writes(self):
        values = ["sb_secret_" + "a" * 20, "prefix:sb_secret_" + "b" * 12,
                  "Bearer short", "prefix:github_pat_" + "c" * 12,
                  "prefix:sk-" + "d" * 16, "xoxb-" + "e" * 12,
                  "eyJ" + "f" * 20 + "." + "g" * 10 + "." + "h" * 10,
                  "-----BEGIN PRIVATE KEY-----"]
        for index, value in enumerate(values):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.tasks.start_task(task_payload(request_id="secret-" + str(index),
                    context={"user-stated": {"note": value}}))
        self.assertEqual(self.tasks.list_tasks(), [])
        task = self.tasks.start_task(task_payload(request_id="secret-evidence"))
        before = self.tasks.get_task(task["task_id"])
        with self.assertRaises(ValidationError):
            self.tasks.append_evidence(task["task_id"], "secret-event", "context",
                "agent-reported", {"note": values[0]})
        with self.assertRaises(ValidationError):
            self.tasks.finish_task(task["task_id"], "secret-finish", values[0])
        self.assertEqual(self.tasks.get_task(task["task_id"]), before)

    def test_imported_legacy_secret_values_are_sanitized(self):
        value = "sb_secret_" + "syntheticvalue12345678"
        card = json.loads(json.dumps(CARD))
        card["context"]["note"] = value
        self.store.request_job(card)
        saved = full_payload()
        saved["context"] = card["context"]
        self.store.save_rating(saved)
        data_dir = self.store.data_dir
        self.store.close()
        self.store = ReviewStore.open(data_dir)
        self.tasks = self.store.tasks
        imported = self.tasks.get_task("legacy:job-001")
        self.assertNotIn(value, json.dumps(imported))
        self.assertIn("[redacted imported value]", json.dumps(imported))

    def test_ambiguous_legacy_finish_shaped_ids_fail_closed_without_mutation(self):
        task = self.tasks.start_task(task_payload(request_id="ambiguous-legacy"))
        task_id = task["task_id"]
        finished = self.tasks.finish_task(task_id, "old-finish", "Done")
        row = self.tasks.db.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event_id=?",
            (task_id, "old-finish")).fetchone()
        payload = json.loads(row[0])
        payload.pop("operation", None)
        self.tasks.db.execute(
            "UPDATE task_events SET payload_json=? WHERE task_id=? AND event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), task_id, "old-finish"))
        # Both operations could have produced this old row. Never guess ownership.
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            self.tasks.finish_task(task_id, "old-finish", "Done")
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            self.tasks.append_evidence(task_id, "old-finish", "milestone", "agent-reported",
                {"result": "Done", "verification": None, "interrupted": False})
        self.assertEqual(self.tasks.get_task(task_id), finished)

    def test_share_projects_and_redacts_array_paths_and_includes_context_evidence_and_result(self):
        task = self.tasks.start_task(task_payload())
        self.tasks.append_evidence(task["task_id"], "context-1", "context",
                                   "user-stated", {"preference": "brief"})
        self.tasks.append_evidence(task["task_id"], "context-runtime", "context",
                                   "runtime-observed", {"window": "current"})
        self.tasks.append_evidence(task["task_id"], "artifact-1", "artifact",
                                   "agent-reported", {"name": "result.txt", "path": "/Users/alice/private/result.txt"})
        finished = self.tasks.finish_task(task["task_id"], "finish", "Delivered")
        self.tasks.save_review(task["task_id"], {
            "completion": "yes", "ratings": {"overall": 5},
            "gap_reasons": [], "revision": finished["revision"]})
        preview = self.tasks.prepare_share(task["task_id"],
            ["task_text.result", "context.evidence.0.data.preference",
             "artifacts.0.data.name", "artifacts.0.data.path"],
            {"artifacts.0.data.name": "final.txt"})
        details = preview["payload"]["details"]
        self.assertEqual(preview["payload"]["coverage"]["process"], "agent-reported")
        self.assertEqual(details["task_text"]["result"], "Delivered")
        self.assertEqual(details["context"]["evidence"][0]["data"]["preference"], "brief")
        self.assertEqual(details["artifacts"][0]["data"]["name"], "final.txt")
        self.assertNotIn("path", details["artifacts"][0]["data"])
        self.assertNotIn("setup", details)
        setup_preview = self.tasks.prepare_share(task["task_id"], ["setup.model"])
        self.assertEqual(setup_preview["payload"]["details"], {"setup": {"model": "model-x"}})

    def test_stale_review_has_distinct_conflict_error(self):
        task = self.tasks.start_task(task_payload())
        self.tasks.append_evidence(task["task_id"], "new-context", "context",
                                   "user-stated", {"extra": True})
        finished = self.tasks.finish_task(task["task_id"], "stale-finish", "Done")
        from task_schema import StaleTaskRevisionError
        with self.assertRaises(StaleTaskRevisionError):
            self.tasks.save_review(task["task_id"], {
                "completion": "yes", "ratings": {"overall": 4},
                "gap_reasons": [], "revision": task["revision"]})
        self.assertNotEqual(task["revision"], finished["revision"])

    def test_invalid_task_ids_and_status_filters_raise_validation_errors(self):
        for invalid in ({"id": 1}, [], None):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.tasks.get_task(invalid)
        with self.assertRaises(ValidationError):
            self.tasks.list_tasks(status={"unexpected": True})

    def test_browser_route_decodes_legacy_task_id_segments(self):
        from local_service import decode_path_segment
        self.assertEqual(decode_path_segment("legacy%3Ajob-001"), "legacy:job-001")

    def test_get_setup_mcp_tool_reads_saved_preferences(self):
        from unittest.mock import patch
        import mcp_server
        import local_service
        expected = {"mode": "newcomer", "runtime": "Codex",
                    "capabilities": {"task_lifecycle": "available",
                                     "runtime_capture": "manual",
                                     "installation": "not_performed"}}
        tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "get_setup")
        self.assertEqual(tool["inputSchema"], {
            "type": "object", "additionalProperties": False, "properties": {}})
        self.assertEqual(mcp_server.SERVER_INFO["version"], "0.6.0")
        self.assertEqual(local_service.SERVICE_VERSION, "0.6.0")
        with patch.object(mcp_server, "service_call", return_value=expected) as call:
            self.assertEqual(mcp_server.get_setup({}), expected)
        call.assert_called_once_with("get_setup", {})

    def test_credentials_rejected_without_transcript_scanning(self):
        with self.assertRaises(ValidationError):
            self.tasks.start_task(task_payload(context={"user-stated": {
                "api_key": "sk-example-secret-value"}}))
        with self.assertRaises(ValidationError):
            self.tasks.start_task(task_payload(request_id="bad-auth", context={
                "user-stated": {"authorization": "Bearer secret-value-here"}}))
        self.assertEqual(self.tasks.start_task(task_payload(
            request_id="author-prose", context={"user-stated": {
                "author": "author says hello",
                "authentication_overview": "Authentication overview"}}))[
            "context"]["user-stated"]["authentication_overview"], "Authentication overview")

    def test_share_drops_account_and_runtime_auth_identifiers(self):
        task = self.tasks.start_task(task_payload(
            request_id="share-identifiers",
            context={"user-stated": {"account_id": "business-account",
                                     "accountId": "camel-account",
                                     "account_uuid": "uuid-account",
                                     "author": "ordinary prose",
                                     "session_id": "runtime-session",
                                     "service_auth_id": "service-auth"}}))
        finished = self.tasks.finish_task(task["task_id"], "identifier-finish", "Done")
        self.tasks.save_review(task["task_id"], {"completion": "yes",
            "ratings": {"overall": 4}, "gap_reasons": [], "revision": finished["revision"]})
        private = self.tasks.get_task(task["task_id"])
        self.assertEqual(private["context"]["user-stated"]["account_id"], "business-account")
        preview = self.tasks.prepare_share(task["task_id"], ["context"])
        exported = json.dumps(preview["payload"]["details"]["context"])
        for identifier in ("business-account", "camel-account", "uuid-account",
                           "runtime-session", "service-auth"):
            self.assertNotIn(identifier, exported)
        self.assertIn("ordinary prose", exported)

    def test_share_filter_matches_community_private_key_policy(self):
        context = {"id": "private-id", "uuid": "private-uuid",
                   "user_id": "private-user", "oauth": "private-oauth",
                   "email": "private@example.test", "author": "author text",
                   "authentication_doc": "Authentication overview",
                   "tokens": {"value": 12, "unit": "tokens"}}
        task = self.tasks.start_task(task_payload(request_id="community-filter",
                                                  context={"user-stated": {"safe": "baseline"}}))
        # Older local records may contain metadata fields that current writers reject.
        stored = self.tasks._load(task["task_id"])
        stored["context"] = {"user-stated": context}
        self.tasks._save(stored)
        self.assertEqual(self.tasks.get_task(task["task_id"])["context"]["user-stated"], context)
        finished = self.tasks.finish_task(task["task_id"], "community-finish", "Done")
        self.tasks.save_review(task["task_id"], {"completion": "yes",
            "ratings": {"overall": 4}, "gap_reasons": [], "revision": finished["revision"]})

        default_preview = self.tasks.prepare_share(task["task_id"])
        self.assertNotIn("context", default_preview["payload"]["details"])
        self.assertIn("context", default_preview["payload"]["withheld"])
        preview = self.tasks.prepare_share(task["task_id"], ["context"])
        exported = json.dumps(preview["payload"]["details"]["context"])
        for private_value in ("private-id", "private-uuid", "private-user",
                              "private-oauth", "private@example.test"):
            self.assertNotIn(private_value, exported)
        for shared_value in ("author text", "Authentication overview", '"value": 12', '"unit": "tokens"'):
            self.assertIn(shared_value, exported)

    def test_share_preview_created_at_is_frozen_across_restart(self):
        task = self.tasks.start_task(task_payload(request_id="frozen-created-at"))
        finished = self.tasks.finish_task(task["task_id"], "frozen-finish", "Done")
        self.tasks.save_review(task["task_id"], {"completion": "yes",
            "ratings": {"overall": 4}, "gap_reasons": [], "revision": finished["revision"]})
        preview = self.tasks.prepare_share(task["task_id"], ["task_text"])
        self.assertTrue(preview.get("created_at"))
        data_dir = self.store.data_dir
        self.store.close()
        self.store = ReviewStore.open(data_dir)
        self.tasks = self.store.tasks
        frozen = self.tasks.get_share_preview(preview["preview_id"])
        self.assertEqual(frozen["created_at"], preview["created_at"])
        self.assertEqual(frozen["payload"], preview["payload"])

    def test_list_tasks_returns_private_safe_summaries_while_get_task_is_full(self):
        sentinel = "private-context-sentinel-9d3"
        task = self.tasks.start_task(task_payload(
            request_id="private-list", goal="Sensitive goal sent only to get_task",
            context={"user-stated": {"private_note": sentinel}},
            harness={"mode": "existing", "name": "APR", "version": "1",
                     "roles": ["agent"], "loops": ["edit"], "rules": ["preserve"],
                     "tools": ["shell"], "context_categories": ["repo"],
                     "definitions": {"private_setting": sentinel}}))
        self.tasks.append_evidence(task["task_id"], "private-resource", "resource",
                                   "runtime-observed", {"cost": {"value": 1.25, "unit": "USD"}})
        full = self.tasks.get_task(task["task_id"])
        summaries = self.tasks.list_tasks()
        summary = next(item for item in summaries if item["task_id"] == task["task_id"])
        encoded = json.dumps(summary)
        self.assertNotIn(sentinel, encoded)
        self.assertNotIn("Sensitive goal sent only to get_task", encoded)
        self.assertNotIn("context", summary)
        self.assertNotIn("definitions", json.dumps(summary.get("harness", {})))
        self.assertEqual(summary["resources"]["resource_0"]["cost"]["value"], 1.25)
        self.assertEqual(full["context"]["user-stated"]["private_note"], sentinel)
        self.assertEqual(full["harness"]["definitions"]["private_setting"], sentinel)

    def test_parent_link_and_criteria_revisions_are_explicit(self):
        parent = self.tasks.start_task(task_payload(request_id="parent"))
        child = self.tasks.start_task(task_payload(request_id="child",
                                                   parent_task_id=parent["task_id"]))
        self.assertEqual(child["parent_task_id"], parent["task_id"])
        self.tasks.append_evidence(parent["task_id"], "criteria-1", "criteria",
                                   "user-stated", {"success_criteria": ["New criterion"]})
        got = self.tasks.get_task(parent["task_id"])
        self.assertEqual(got["success_criteria"], ["A task can be reviewed"])
        self.assertEqual(got["evidence"][-1]["data"]["success_criteria"], ["New criterion"])

    def test_baseline_source_is_explicit_and_validated(self):
        task = self.tasks.start_task(task_payload(request_id="user-baseline",
                                                  baseline_source="user-stated"))
        self.assertEqual(task["baseline_source"], "user-stated")
        with self.assertRaises(ValidationError):
            self.tasks.start_task(task_payload(request_id="bad-baseline",
                                               baseline_source="runtime-inferred"))

    def test_share_preview_is_frozen_and_contains_only_selected_fields(self):
        task = self.tasks.start_task(task_payload())
        finished = self.tasks.finish_task(task["task_id"], "finish", "Done")
        reviewed = self.tasks.save_review(task["task_id"], {
            "completion": "yes", "ratings": {"overall": 5},
            "gap_reasons": [], "revision": finished["revision"]})
        preview = self.tasks.prepare_share(task["task_id"], ["task_text", "context"])
        payload = preview["payload"]
        self.assertNotIn("task_id", payload)
        self.assertEqual(payload["ratings"], {"overall": 5})
        self.assertEqual(set(payload["details"]), {"task_text", "context"})
        self.tasks.append_evidence(task["task_id"], "later", "context",
                                   "agent-reported", {"text": "new"})
        frozen = self.tasks.get_share_preview(preview["preview_id"])
        self.assertEqual(frozen["payload"], payload)
        self.assertEqual(reviewed["status"], "reviewed")
        current = self.tasks.finish_task(task["task_id"], "follow-up", "More work")
        self.assertEqual(current["status"], "reviewed")
        self.assertEqual(current["result"], "Done")
        self.assertEqual(len(current["evidence"]), len(reviewed["evidence"]))
        self.assertEqual(len(current["post_review_evidence"]), 2)

    def test_legacy_review_is_backfilled_without_baseline_claim(self):
        self.store.request_job(dict(CARD))
        self.store.save_rating(full_payload())
        data_dir = self.store.data_dir
        self.store.close()
        # Model a pre-task database that contains the old review schema only.
        db = __import__("sqlite3").connect(str(data_dir / "reviews.db"))
        for table in ("task_share_previews", "task_reviews", "task_events",
                      "task_setup", "task_records"):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_migrations WHERE version=4")
        db.commit()
        db.close()
        self.store = ReviewStore.open(data_dir)
        self.tasks = self.store.tasks
        versions = self.store.migration_versions()
        self.assertIn(4, versions)
        rows = self.tasks.list_tasks(status="reviewed")
        legacy = next(row for row in rows if row["task_id"] == "legacy:job-001")
        from local_service import decode_path_segment
        self.assertEqual(self.tasks.get_task(decode_path_segment("legacy%3Ajob-001"))["task_id"],
                         "legacy:job-001")
        self.assertTrue(legacy["baseline_missing"])
        self.assertIsNone(legacy["harness"])
        self.assertEqual(legacy["review"]["completion"], "yes")
        new = self.tasks.start_task(task_payload(request_id="summary-new"))
        finished = self.tasks.finish_task(new["task_id"], "summary-finish", "Done")
        self.tasks.save_review(new["task_id"], {
            "completion": "yes", "ratings": {"overall": 3, "quality": 2},
            "gap_reasons": [], "revision": finished["revision"]})
        summary = self.store.summary("coding")
        self.assertEqual(summary["count"], 2)  # legacy job + new task, no backfill duplicate
        self.assertEqual(summary["mean_ratings"]["overall"], 4.0)
        self.assertEqual(summary["rating_counts"], {
            "overall": 2, "quality": 2, "speed": 1, "resource_efficiency": 1})
        self.assertEqual(summary["mean_ratings"]["speed"], 3)


if __name__ == "__main__":
    unittest.main()
