"""Deterministic P7.3 tests against the committed Second Shift state API."""

from __future__ import annotations

import copy
import shutil
import tempfile
import threading
import unittest

from second_shift.pool_recovery import check_pool_recovery, pool_recovery_status
from second_shift.state import StateFile


def registry(count: int, *, recovery_active: bool = False) -> dict:
    models = [
        {"id": f"original-{i}", "provider": "test", "role": "coding", "status": "active", "credit_status": "available"}
        for i in range(count)
    ]
    models += [
        {"id": "copilot", "provider": "copilot", "role": "recovery-coding", "status": "active" if recovery_active else "standby", "credit_status": "unknown"},
        {"id": "codex", "provider": "codex", "role": "recovery-coding", "status": "active" if recovery_active else "standby", "credit_status": "unknown"},
    ]
    return {"models": models}


class TestP73PoolRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(self.temp_dir)
        self.state.save_registry(registry(5))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def observe(self):
        return check_pool_recovery(self.state)

    def set_count(self, count: int):
        current = self.state.get_registry()
        for model in current["models"]:
            if model["id"].startswith("original-"):
                model["status"] = "active" if int(model["id"].split("-")[-1]) < count else "standby"
        self.state.save_registry(current)

    def test_drop_to_two_activates_copilot_then_codex(self):
        self.observe()  # establish the five-member baseline
        self.set_count(2)
        updated, event = self.observe()
        self.assertEqual(event["actions_taken"], ["activate_copilot"])
        self.assertEqual(updated["pool_recovery"]["transitions_to_two"], 1)
        self.set_count(2)  # two originals + Copilot = three usable
        self.observe()
        self.set_count(1)  # one original + Copilot = two usable
        updated, event = self.observe()
        self.assertEqual(event["actions_taken"], ["activate_codex"])
        self.assertEqual(updated["pool_recovery"]["transitions_to_two"], 2)

    def test_repeated_observation_does_not_flap(self):
        self.observe()
        self.set_count(2)
        _, first = self.observe()
        _, second = self.observe()
        self.assertEqual(first["actions_taken"], ["activate_copilot"])
        self.assertEqual(second["actions_taken"], [])
        self.assertEqual(pool_recovery_status(self.state)["transitions_to_two"], 1)

    def test_rise_to_four_deactivates_codex_then_copilot(self):
        self.observe()
        self.set_count(2)
        self.observe()
        self.set_count(2)  # rise to three usable before the second drop
        self.observe()
        self.set_count(1)
        self.observe()
        # Recovery members bring the pool to four active members after the
        # second drop; the first rise to four removes Codex.
        self.set_count(3)  # three originals + both recovery members = five
        updated, event = self.observe()
        self.assertEqual(event["actions_taken"], ["deactivate_codex"])
        self.assertNotIn("codex", updated["pool_recovery"]["recovery_members"])
        self.set_count(2)  # two originals + Copilot = three usable
        self.observe()
        self.set_count(3)  # three originals + Copilot = four usable
        updated, event = self.observe()
        self.assertEqual(event["actions_taken"], ["deactivate_copilot"])
        self.assertEqual(updated["pool_recovery"]["recovery_members"], [])

    def test_exhausted_and_unknown_credit_semantics(self):
        current = self.state.get_registry()
        current["models"][0]["credit_status"] = "exhausted"
        current["models"][1]["credit_status"] = "unknown"
        self.state.save_registry(current)
        self.observe()
        self.assertEqual(pool_recovery_status(self.state)["usable_member_count"], 4)

    def test_persisted_events_and_concurrent_observation(self):
        self.observe()
        self.set_count(2)
        errors = []

        def run():
            try:
                self.observe()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        state = pool_recovery_status(self.state)
        self.assertEqual(state["transitions_to_two"], 1)
        self.assertEqual(len(state["transition_events"]), 1)


if __name__ == "__main__":
    unittest.main()
