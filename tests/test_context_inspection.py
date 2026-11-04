"""Inspector trust, projection and review snapshot boundaries in a disposable store."""
import json
import unittest

from inspector_schema import task_inspection
from review_store import ReviewStore
from task_schema import ValidationError
from tests.helpers import fresh_data_dir
from tests.test_tasks import task_payload


class InspectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ReviewStore.open(fresh_data_dir(self))
        self.addCleanup(self.store.close)
        self.api = self.store.tasks
        self.id = self.api.start_task(task_payload())["task_id"]
        self.item = {"path": "private-operator-guide.md", "bytes": 2, "sha256": "a" * 64}
        self.inv = {"type": "inventory", "root_id": "b" * 64, "items": [self.item]}
        self.turn = {"type": "turn", "turn": 1, "session_ref": "c" * 64,
                     "stream_ref": "d" * 64, "reads": [self.item], "gaps": []}

    def test_no_agent_or_import_can_write_receipts(self):
        for source in ("agent-reported", "imported", "runtime-observed"):
            with self.assertRaises(ValidationError):
                self.api.append_evidence(self.id, "inspector.inventory", "inspector", source, self.inv)
        self.api.append_evidence(self.id, "claim", "context", "runtime-observed", self.inv)
        record = task_inspection(self.api.get_task(self.id))
        self.assertIsNone(record["capture"]["inventory"])
        self.assertEqual(record["capture"]["turns"], [])
        self.assertEqual(record["capture"]["reports"][0]["event_id"], "claim")

    def test_require_one_inventory_matching_reads_and_one_session(self):
        append = lambda turn: self.api.append_inspector(self.id, "inspector.turn." + str(turn["turn"]), turn)
        with self.assertRaises(ValidationError):
            append(self.turn)
        self.api.append_inspector(self.id, "inspector.inventory", self.inv)
        for mutation in ({"reads": [{**self.item, "bytes": 0}]},
                         {"reads": [{**self.item, "body": "unfiltered"}]},
                         {"gaps": ["raw-output"]}):
            with self.assertRaises(ValidationError):
                append({**self.turn, **mutation})
        append(self.turn)
        with self.assertRaises(ValidationError):
            append({**self.turn, "session_ref": "e"*64, "stream_ref": "f"*64, "turn": 2})

    def test_frozen_rating_and_sharing_exclude_inspector_payloads(self):
        self.api.append_inspector(self.id, "inspector.inventory", self.inv)
        self.api.append_inspector(self.id, "inspector.turn.1", self.turn)
        t = self.api.finish_task(self.id, "done", "Fixture result.")
        self.api.save_review(self.id, {"ratings": {"overall": 2}, "completion": "no",
                                        "gap_reasons": [], "revision": t["revision"]})
        self.api.append_inspector(self.id, "inspector.turn.2",
                                  {**self.turn, "turn": 2, "stream_ref": "e"*64})
        record = task_inspection(self.api.get_task(self.id))
        self.assertEqual([x["turn"] for x in record["capture"]["turns"]], [1])
        self.assertEqual([x["turn"] for x in record["after_review"]["turns"]], [2])
        self.assertEqual(record["review"]["ratings"], {"overall": 2})
        share = self.api.prepare_share(self.id, ["context", "process", "setup", "artifacts"])
        self.assertNotIn(self.item["path"], json.dumps(share))
        self.assertNotIn(self.item["sha256"], json.dumps(share))


if __name__ == "__main__":
    unittest.main()
