"""Acceptance tests for the P7.1 durable state boundary."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest

from second_shift.ledger import FailureLedger
from second_shift.registry import ModelRegistry
from second_shift.state import StateFile


class TestP71Contract(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(self.temp_dir)
        self.ledger = FailureLedger(self.state)
        self.registry = {"models": [{"id": "m1", "provider": "p", "status": "active"}]}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _record(self, classification, goal_id):
        return self.ledger.record_failure(
            model_id="m1", provider="p", goal_id=goal_id, goal_class="coding",
            classification=classification, evidence=[f"evidence/{goal_id}"],
            registry=self.registry,
        )

    def test_partial_code_counts_and_reaches_quarantine(self):
        for index in range(3):
            self._record("partial_code", f"g{index}")
        registry = ModelRegistry(self.state).load_registry()
        model = ModelRegistry(self.state).get_model("m1", registry)
        self.assertEqual(model["failure_counts_by_goal"]["coding"], 3)
        self.assertEqual(model["status"], "unusable")

    def test_operational_and_no_code_events_preserve_status(self):
        self._record("timeout", "timeout")
        self._record("no_code", "no-code")
        self._record("unknown", "unknown")
        registry = ModelRegistry(self.state).load_registry()
        model = ModelRegistry(self.state).get_model("m1", registry)
        self.assertEqual(model["status"], "active")
        self.assertNotIn("failure_counts_by_goal", model)
        self.assertEqual(len(self.state.get_ledger_entries()), 3)

    def test_reactivation_requires_persisted_reviewer_evidence(self):
        registry = {"models": [{"id": "m1", "status": "unusable"}]}
        manager = ModelRegistry(self.state)
        with self.assertRaises(ValueError):
            manager.reactivate_model("m1", "reviewer", registry)
        updated = manager.reactivate_model(
            "m1", "reviewer", registry, reviewer_evidence="review/p71-evidence"
        )
        self.assertEqual(updated["models"][0]["reviewer_evidence"], "review/p71-evidence")

    def test_success_resets_only_goal_streak_and_persists_artifact(self):
        self._record("incorrect_code", "failed")
        registry = self.ledger.record_success(
            model_id="m1", goal_class="coding", artifact="artifact/good",
            registry=self.registry,
        )
        model = ModelRegistry(self.state).get_model("m1", registry)
        self.assertEqual(model["failure_counts_by_goal"]["coding"], 0)
        self.assertEqual(model["last_successful_artifact"], "artifact/good")
        self.assertEqual(ModelRegistry(self.state).load_registry(), registry)

    def test_concurrent_transactions_do_not_lose_failures(self):
        errors = []

        def worker(index):
            try:
                self._record("incorrect_code", f"concurrent-{index}")
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.state.get_ledger_entries()), 8)
        registry = ModelRegistry(self.state).load_registry()
        self.assertEqual(
            ModelRegistry(self.state).get_substantive_failure_count("m1", "coding", registry),
            8,
        )


if __name__ == "__main__":
    unittest.main()
