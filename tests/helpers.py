"""Shared helpers: isolated data dirs, synthetic fixtures, no home access."""
import json
import os
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh_data_dir(testcase):
    tmp = tempfile.TemporaryDirectory(prefix="apr-test-")
    testcase.addCleanup(tmp.cleanup)
    return tmp.name


CARD = {
    "review_id": "job-001",
    "job_type": "coding",
    "task": "Fix the login redirect loop",
    "action": "Patched the session refresh path",
    "result": "Redirect loop gone in manual check",
    "verification": "partial",
    "context": {"model": "synthetic-model"},
    "resources": {"elapsed_time": 42},
}

FULL = dict(CARD, completion="yes",
             ratings={"overall": 5, "quality": 4, "speed": 3, "resource_efficiency": 4})


def full_payload(**overrides):
    payload = dict(FULL, ratings=dict(FULL["ratings"]))
    payload.update(overrides)
    return payload


def browser_payload(revision, **overrides):
    """Only the fields the browser UI declares: completion, gap_reasons,
    ratings, revision (plus {"skip": True} for skips)."""
    payload = {"completion": "yes", "gap_reasons": [],
               "ratings": dict(FULL["ratings"]), "revision": revision}
    payload.update(overrides)
    return payload


def jsonl_record(review_id="legacy-1", **overrides):
    record = dict(CARD)
    record["review_id"] = review_id
    record.update(full_payload())
    record["review_id"] = review_id
    record["recorded_at"] = "2026-01-02T03:04:05+00:00"
    for key, value in overrides.items():
        record[key] = value
    return record


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path
