"""Acceptance scenarios for task boundaries, delegation, and measured outcomes."""
import unittest

from review_store import ReviewStore
from tests.helpers import fresh_data_dir


def start_payload(request_id, title, parent_task_id=None):
    return {
        "request_id": request_id,
        "title": title,
        "category": "coding",
        "goal": title,
        "success_criteria": ["The requested work is complete"],
        "runtime": "synthetic-runtime",
        "model": "synthetic-model",
        "harness": {"mode": "existing", "name": "APR", "version": "1",
                    "roles": ["agent"], "loops": ["edit"], "rules": [],
                    "tools": ["synthetic"], "context_categories": [],
                    "definitions": {}},
        "parent_task_id": parent_task_id,
        "context": {"user-stated": {"goal": title}},
        "baseline_missing": False,
        "baseline_source": "user-stated",
    }


class TaskScenariosTest(unittest.TestCase):
    def setUp(self):
        self.store = ReviewStore.open(fresh_data_dir(self))
        self.tasks = self.store.tasks

    def tearDown(self):
        self.store.close()

    def finish_and_review(self, task, event_id, result="Completed", ratings=None):
        finished = self.tasks.finish_task(task["task_id"], event_id, result)
        reviewed = self.tasks.save_review(task["task_id"], {
            "completion": "yes",
            "ratings": ratings or {"overall": 5},
            "gap_reasons": [],
            "revision": finished["revision"],
        })
        return reviewed

    def test_one_long_automation_is_one_task_with_one_human_review(self):
        task = self.tasks.start_task(start_payload("automation-long", "Long automation"))
        for turn in range(50):
            self.tasks.append_evidence(task["task_id"], f"turn-{turn}", "milestone",
                                       "agent-reported", {"turn": turn, "note": "synthetic progress"})

        finished = self.tasks.finish_task(task["task_id"], "automation-finish", "Completed all 50 turns")
        self.assertEqual(finished["status"], "awaiting_review")
        self.assertIsNone(finished["review"])
        reviewed = self.tasks.save_review(task["task_id"], {
            "completion": "yes", "ratings": {"overall": 5}, "gap_reasons": [],
            "revision": finished["revision"],
        })

        tasks = self.tasks.list_tasks()
        self.assertEqual([item["task_id"] for item in tasks], [task["task_id"]])
        full = self.tasks.get_task(task["task_id"])
        self.assertEqual(len(full["evidence"]), 50 + 1)
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["review"]["ratings"], {"overall": 5})

    def test_distinct_jobs_in_one_conversation_remain_isolated_when_interleaved(self):
        # Conversation identity is intentionally not needed to combine jobs: separate
        # start requests create separate reviewable records even while interleaved.
        first = self.tasks.start_task(start_payload("conversation-job-a", "Job A"))
        second = self.tasks.start_task(start_payload("conversation-job-b", "Job B"))
        self.tasks.append_evidence(first["task_id"], "a-context", "context",
                                   "user-stated", {"marker": "only-a"})
        self.tasks.append_evidence(second["task_id"], "b-context", "context",
                                   "user-stated", {"marker": "only-b"})
        self.tasks.append_evidence(first["task_id"], "a-resource", "resource",
                                   "runtime-observed", {"calls": {"value": 3, "unit": "calls"}})
        self.tasks.append_evidence(second["task_id"], "b-resource", "resource",
                                   "runtime-observed", {"calls": {"value": 8, "unit": "calls"}})

        reviewed_first = self.finish_and_review(first, "a-finish", "A done")
        reviewed_second = self.finish_and_review(second, "b-finish", "B done")
        record_a = self.tasks.get_task(first["task_id"])
        record_b = self.tasks.get_task(second["task_id"])
        self.assertNotEqual(record_a["task_id"], record_b["task_id"])
        self.assertEqual(record_a["review"]["completion"], "yes")
        self.assertEqual(record_b["review"]["completion"], "yes")
        self.assertIn("only-a", str(record_a["evidence"]))
        self.assertNotIn("only-b", str(record_a["evidence"]))
        self.assertIn("only-b", str(record_b["evidence"]))
        self.assertNotIn("only-a", str(record_b["evidence"]))
        self.assertEqual(record_a["resources"]["a-resource"]["calls"]["value"], 3)
        self.assertEqual(record_b["resources"]["b-resource"]["calls"]["value"], 8)
        self.assertEqual(reviewed_first["status"], "reviewed")
        self.assertEqual(reviewed_second["status"], "reviewed")

    def test_delegated_child_measurements_are_not_added_to_parent(self):
        parent = self.tasks.start_task(start_payload("delegation-parent", "Parent task"))
        child = self.tasks.start_task(start_payload("delegation-child", "Child task",
                                                    parent_task_id=parent["task_id"]))
        self.tasks.append_evidence(parent["task_id"], "parent-cost", "resource",
                                   "runtime-observed", {"cost": {"value": 2, "unit": "USD"}})
        self.tasks.append_evidence(child["task_id"], "child-cost", "resource",
                                   "runtime-observed", {"cost": {"value": 7, "unit": "USD"}})

        parent_record = self.tasks.get_task(parent["task_id"])
        child_record = self.tasks.get_task(child["task_id"])
        self.assertEqual(child_record["parent_task_id"], parent["task_id"])
        self.assertEqual(parent_record["resources"], {
            "parent-cost": {"cost": {"value": 2, "unit": "USD"}, "source": "runtime-observed"}})
        self.assertEqual(child_record["resources"], {
            "child-cost": {"cost": {"value": 7, "unit": "USD"}, "source": "runtime-observed"}})

    def test_success_ratings_do_not_erase_distinct_measured_costs(self):
        ratings = {"overall": 5, "quality": 5, "speed": 5, "resource_efficiency": 5}
        efficient = self.tasks.start_task(start_payload("efficient-success", "Efficient success"))
        expensive = self.tasks.start_task(start_payload("expensive-success", "Expensive success"))
        self.tasks.append_evidence(efficient["task_id"], "efficient-meter", "resource",
                                   "runtime-observed", {"cost": {"value": 0.25, "unit": "USD"}})
        self.tasks.append_evidence(expensive["task_id"], "expensive-meter", "resource",
                                   "runtime-observed", {"cost": {"value": 25, "unit": "USD"}})

        efficient_record = self.finish_and_review(efficient, "efficient-finish", ratings=ratings)
        expensive_record = self.finish_and_review(expensive, "expensive-finish", ratings=ratings)
        self.assertEqual(efficient_record["review"]["ratings"], expensive_record["review"]["ratings"])
        self.assertEqual(efficient_record["review"]["completion"], "yes")
        self.assertEqual(expensive_record["review"]["completion"], "yes")
        self.assertEqual(efficient_record["resources"]["efficient-meter"]["cost"]["value"], 0.25)
        self.assertEqual(expensive_record["resources"]["expensive-meter"]["cost"]["value"], 25)

    def test_agent_finish_stays_awaiting_human_review_until_explicit_review(self):
        task = self.tasks.start_task(start_payload("no-auto-review", "No auto review"))
        finished = self.tasks.finish_task(task["task_id"], "finish-no-auto-review", "Done")
        self.assertEqual(finished["status"], "awaiting_review")
        self.assertIsNone(finished["review"])
        self.assertEqual(self.tasks.get_task(task["task_id"])["review"], None)

    def test_share_withholds_bare_temp_paths_but_preserves_urls_and_local_evidence(self):
        artifacts = {"tmp": "/tmp/agent-output/report.json",
                     "var_tmp": "/var/tmp/agent-output/report.json",
                     "url": "https://example.test/path/to/report"}
        task = self.tasks.start_task(start_payload("share-temp-paths", "Share temp paths"))
        self.tasks.append_evidence(task["task_id"], "artifact-paths", "artifact",
                                   "agent-reported", artifacts)
        finished = self.tasks.finish_task(task["task_id"], "finish-temp-paths", "Done")
        self.tasks.save_review(task["task_id"], {
            "completion": "yes", "ratings": {"overall": 5}, "gap_reasons": [],
            "revision": finished["revision"],
        })

        private = self.tasks.get_task(task["task_id"])
        preview = self.tasks.prepare_share(task["task_id"], ["artifacts"])
        public_artifact = preview["payload"]["details"]["artifacts"][0]["data"]
        self.assertEqual(private["evidence"][0]["data"], artifacts)
        self.assertEqual(public_artifact["tmp"], "[local path withheld]")
        self.assertEqual(public_artifact["var_tmp"], "[local path withheld]")
        self.assertEqual(public_artifact["url"], "https://example.test/path/to/report")


if __name__ == "__main__":
    unittest.main()
