"""Store contract: migrations, perms, transactions, import."""
import json
import os
import stat
import unittest

from review_store import (
    ConflictError,
    ImportAbort,
    ReviewStore,
    StaleRevisionError,
)
from tests.helpers import CARD, fresh_data_dir, full_payload, jsonl_record, write_jsonl


class StoreBasicsTest(unittest.TestCase):
    def test_migrations_tracked_and_rerunnable(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            versions = store.migration_versions()
            self.assertEqual(versions, sorted(versions))
            self.assertTrue(versions)
            store.apply_migrations()
            self.assertEqual(versions, store.migration_versions())
        finally:
            store.close()

    def test_private_file_modes(self):
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            mode = stat.S_IMODE(os.stat(store.db_path).st_mode)
            self.assertEqual(mode, 0o600)
            dmode = stat.S_IMODE(os.stat(data_dir).st_mode)
            self.assertEqual(dmode, 0o700)
        finally:
            store.close()

    def test_same_token_stable_and_snapshot_refreshes(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            token1, rev1, _ = store.request_job(dict(CARD))
            updated = dict(CARD, task="Updated cumulative summary")
            token2, rev2, _ = store.request_job(updated)
            self.assertEqual(token1, token2)
            self.assertGreater(rev2, rev1)
            card = store.get_card(token1)
            self.assertEqual(card["snapshot"]["task"], "Updated cumulative summary")
            self.assertEqual(card["revision"], rev2)
        finally:
            store.close()


class SaveSemanticsTest(unittest.TestCase):
    def test_identical_retry_returns_same_receipt(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD))
            first = store.save_rating(full_payload())
            second = store.save_rating(full_payload())
            self.assertEqual(first["status"], "recorded")
            self.assertEqual(second["status"], "already_recorded")
            self.assertEqual(first["recorded_at"], second["recorded_at"])
            self.assertEqual(store.saved_count(), 1)
        finally:
            store.close()

    def test_conflicting_retry_aborts_without_overwrite(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD))
            store.save_rating(full_payload())
            changed = full_payload(ratings={"overall": 1, "quality": 1,
                                            "speed": 1, "resource_efficiency": 1})
            with self.assertRaises(ConflictError):
                store.save_rating(changed)
            self.assertEqual(store.saved_count(), 1)
            card = store.get_card_by_review_id("job-001")
            self.assertEqual(card["saved"]["ratings"]["overall"], 5)
        finally:
            store.close()

    def test_saved_snapshot_frozen_after_refresh(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            _, rev, _ = store.request_job(dict(CARD))
            store.save_rating(full_payload())
            # A later request with the same ID must not touch the rated
            # snapshot or bump the revision: saved reviews are frozen.
            _, rev2, created = store.request_job(
                dict(CARD, task="Later work, different scope"))
            self.assertEqual(rev2, rev)
            self.assertFalse(created)
            card = store.get_card_by_review_id("job-001")
            self.assertEqual(card["saved"]["snapshot"]["task"], CARD["task"])
            self.assertEqual(card["snapshot"]["task"], CARD["task"])
        finally:
            store.close()

    def test_stale_revision_rejected(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            _, rev1, _ = store.request_job(dict(CARD))
            _, rev2, _ = store.request_job(dict(CARD, task="newer card"))
            self.assertGreater(rev2, rev1)
            with self.assertRaises(StaleRevisionError):
                store.save_rating(full_payload(), expected_revision=rev1)
            self.assertEqual(store.saved_count(), 0)
            receipt = store.save_rating(full_payload(), expected_revision=rev2)
            self.assertEqual(receipt["status"], "recorded")
        finally:
            store.close()


class ImportTest(unittest.TestCase):
    def test_import_atomic_idempotent_with_provenance(self):
        data_dir = fresh_data_dir(self)
        store = ReviewStore.open(data_dir)
        try:
            legacy = os.path.join(data_dir, "legacy.jsonl")
            write_jsonl(legacy, [jsonl_record("legacy-1"), jsonl_record("legacy-2")])
            first = store.import_jsonl(legacy)
            self.assertEqual(first["imported"], 2)
            second = store.import_jsonl(legacy)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(store.saved_count(), 2)
            card = store.get_card_by_review_id("legacy-1")
            self.assertEqual(card["saved"]["recorded_at"], "2026-01-02T03:04:05+00:00")
            self.assertIn("source_digest", card["saved"])
        finally:
            store.close()

    def test_import_conflict_and_malformed_abort_without_partial_writes(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            store.request_job(dict(CARD))
            store.save_rating(full_payload())
            bad = os.path.join(os.path.dirname(store.db_path), "bad.jsonl")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(jsonl_record("job-001",
                    ratings={"overall": 1, "quality": 1, "speed": 1,
                             "resource_efficiency": 1})) + "\n")
                handle.write("not json at all\n")
            with self.assertRaises(ImportAbort):
                store.import_jsonl(bad)
            self.assertEqual(store.saved_count(), 1)
            card = store.get_card_by_review_id("job-001")
            self.assertEqual(card["saved"]["ratings"]["overall"], 5)
        finally:
            store.close()

    def test_dry_run_writes_nothing(self):
        store = ReviewStore.open(fresh_data_dir(self))
        try:
            legacy = os.path.join(os.path.dirname(store.db_path), "legacy.jsonl")
            write_jsonl(legacy, [jsonl_record("legacy-9")])
            result = store.import_jsonl(legacy, dry_run=True)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(store.saved_count(), 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
