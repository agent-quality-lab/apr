import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desktop_resources import selected_turn_metrics
from review_store import ReviewStore
from tests.helpers import fresh_data_dir
from tests.test_tasks import task_payload


class DesktopResourcesTest(unittest.TestCase):
    def test_selected_turn_is_complete_scoped_and_frozen_at_review(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CODEX_HOME": root}):
            home = Path(root)
            thread = "00000000-0000-0000-0000-000000000001"
            turn = "00000000-0000-0000-0000-000000000002"
            other = "00000000-0000-0000-0000-000000000003"
            scope = {"thread_id": thread, "turn_ids": [turn]}
            path = home / "sessions" / ("rollout-" + thread + ".jsonl")
            path.parent.mkdir()
            db = sqlite3.connect(home / "state_5.sqlite")
            db.execute("CREATE TABLE threads (id, rollout_path)")
            db.execute("INSERT INTO threads VALUES (?,?)", (thread, str(path)))
            db.commit()
            db.close()

            def emit(kind, **payload):
                with path.open("a") as stream:
                    print(json.dumps({"type": kind, "payload": payload}, separators=(",", ":")), file=stream)

            def usage(turn_id, total):
                emit("token_usage_record", turn_id=turn_id, thread_id=thread,
                     turn_token_usage={"total_tokens": total})

            emit("event_msg", type="task_started", turn_id=other)
            usage(other, 4000)
            emit("event_msg", type="task_complete", turn_id=other, duration_ms=9000)
            emit("event_msg", type="task_started", turn_id=turn)
            usage(turn, 6)
            usage(turn, 10)
            for _ in range(2):
                emit("response_item", type="function_call", call_id="same-call")
            store = ReviewStore.open(fresh_data_dir(self))
            self.addCleanup(store.close)
            tasks = store.tasks
            task_id = tasks.start_task(task_payload())["task_id"]
            done = tasks.finish_task(task_id, "done", "Result", verification={"codex_resource_scope": scope})
            self.assertEqual(tasks.get_task(task_id)["resources"], {})
            emit("event_msg", type="task_complete", turn_id=turn, duration_ms=800)
            self.assertIsNone(selected_turn_metrics({"thread_id": "../other", "turn_ids": [turn]}))
            recorded = tasks.get_task(task_id)["resources"]["codex-selected-turns"]
            self.assertEqual(recorded["tokens"]["value"], 10)
            self.assertEqual(recorded["tool_calls"]["value"], 1)
            self.assertEqual(recorded["elapsed_time"]["value"], 800)
            self.assertNotIn("cost", recorded)
            tasks.save_review(task_id, {"completion": "yes", "ratings": {"overall": 4},
                                        "revision": done["revision"], "gap_reasons": []})
            path.unlink()
            self.assertEqual(tasks.get_task(task_id)["resources"]["codex-selected-turns"], recorded)
