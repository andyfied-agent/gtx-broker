"""Deterministic tests for Second Shift failure ledger."""

from __future__ import annotations
import sys
import tempfile
import shutil
from unittest import TestCase, main as unittest_main
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from second_shift.state import StateFile
from second_shift.ledger import FailureLedger, FailureClassification


class TestClassification(TestCase):
    """Tests for failure classification logic."""

    def test_substantive_classifications(self):
        """Test substantive classifications."""
        substantive = [
            ("incorrect_code", True),
            ("tests_failed", True),
        ]

        for classification, expected in substantive:
            ledger = FailureLedger(StateFile(state_dir=tempfile.mkdtemp()))
            result = ledger.classify_as_substantive(classification)
            self.assertEqual(result, expected, f"{classification}")

    def test_non_substantive_classifications(self):
        """Test non-substantive classifications."""
        non_substantive = [
            ("no_code", False),
            ("partial_code", True),
            ("timeout", False),
            ("endpoint_unavailable", False),
            ("authentication", False),
            ("credit_exhausted", False),
            ("rate_limited", False),
            ("policy_refusal", False),
            ("context_limit", False),
        ]

        for classification, expected in non_substantive:
            ledger = FailureLedger(StateFile(state_dir=tempfile.mkdtemp()))
            result = ledger.classify_as_substantive(classification)
            self.assertEqual(result, expected, f"{classification}")

    def test_unknown_classification(self):
        """Unknown evidence is operationally non-substantive."""
        ledger = FailureLedger(StateFile(state_dir=tempfile.mkdtemp()))
        result = ledger.classify_as_substantive("unknown")
        self.assertFalse(result)


class TestRecordFailure(TestCase):
    """Tests for recording failures."""

    def setUp(self):
        """Create test state and ledger with unique temp dir."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.ledger = FailureLedger(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_substantive_failure(self):
        """Test recording a substantive failure."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.ledger.record_failure(
            model_id="m1",
            provider="p1",
            goal_id="g1",
            goal_class="coding",
            classification="tests_failed",
            evidence=["evidence/1"],
            registry=registry,
        )

        # Check ledger entry was created
        entries = self.state.get_ledger_entries()
        self.assertEqual(len(entries), 1)

        entry = entries[0]
        self.assertEqual(entry["model_id"], "m1")
        self.assertEqual(entry["classification"], "tests_failed")
        self.assertTrue(entry["counted_as_substantive_failure"])
        self.assertEqual(entry["consecutive_substantive_failures"], 1)

        # Check registry was updated
        from second_shift.registry import ModelRegistry
        model = ModelRegistry(self.state).get_model("m1", registry)
        self.assertEqual(model.get("status"), "active")  # Not yet quarantined

    def test_record_non_substantive_failure(self):
        """Test recording a non-substantive failure."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.ledger.record_failure(
            model_id="m1",
            provider="p1",
            goal_id="g1",
            goal_class="coding",
            classification="timeout",
            evidence=["evidence/1"],
            registry=registry,
        )

        # Check ledger entry was created
        entries = self.state.get_ledger_entries()
        self.assertEqual(len(entries), 1)

        entry = entries[0]
        self.assertFalse(entry["counted_as_substantive_failure"])
        self.assertEqual(entry["consecutive_substantive_failures"], 0)

        # Check registry was NOT updated with failure count
        from second_shift.registry import ModelRegistry
        count = ModelRegistry(self.state).get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 0)

    def test_record_failure_with_all_fields(self):
        """Test recording failure with all optional fields."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.ledger.record_failure(
            model_id="m1",
            provider="p1",
            goal_id="g1",
            goal_class="coding",
            classification="tests_failed",
            evidence=["evidence/1", "evidence/2"],
            registry=registry,
            artifact="artifact/abc",
            credit_status="low",
            reviewer="reviewer1",
            notes="Additional context",
            pool_recovery_transition=1,
            pool_recovery_action="activate_copilot",
        )

        entries = self.state.get_ledger_entries()
        entry = entries[0]

        self.assertEqual(entry.get("artifact"), "artifact/abc")
        self.assertEqual(entry.get("credit_status"), "low")
        self.assertEqual(entry.get("reviewer"), "reviewer1")
        self.assertEqual(entry.get("notes"), "Additional context")
        self.assertEqual(entry.get("pool_recovery_transition"), 1)
        self.assertEqual(entry.get("pool_recovery_action"), "activate_copilot")


class TestQueryFailures(TestCase):
    """Tests for querying failures."""

    def setUp(self):
        """Create test state and ledger with unique temp dir."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.ledger = FailureLedger(self.state)

        # Pre-populate with test data
        registry1 = {"models": [{"id": "m1", "status": "active"}]}
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g1", goal_class="coding",
            classification="tests_failed", evidence=["e1"], registry=registry1,
        )
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g2", goal_class="coding",
            classification="incorrect_code", evidence=["e2"], registry=registry1,
        )
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g3", goal_class="review",
            classification="tests_failed", evidence=["e3"], registry=registry1,
        )
        registry2 = {"models": [{"id": "m2", "status": "active"}]}
        self.ledger.record_failure(
            model_id="m2", provider="p2", goal_id="g4", goal_class="coding",
            classification="timeout", evidence=["e4"], registry=registry2,
        )

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_failures_for_model(self):
        """Test getting failures for a specific model."""
        failures = self.ledger.get_failures_for_model("m1")
        self.assertEqual(len(failures), 3)

    def test_get_failures_for_model_not_found(self):
        """Test getting failures for non-existent model."""
        failures = self.ledger.get_failures_for_model("nonexistent")
        self.assertEqual(len(failures), 0)

    def test_get_failures_filtered_by_goal_class(self):
        """Test filtering failures by goal class."""
        failures = self.ledger.get_failures_for_model("m1", "coding")
        self.assertEqual(len(failures), 2)

        failures = self.ledger.get_failures_for_model("m1", "review")
        self.assertEqual(len(failures), 1)

    def test_get_substantive_failures(self):
        """Test getting only substantive failures."""
        substantive = self.ledger.get_substantive_failures("m1", "coding")
        self.assertEqual(len(substantive), 2)

        # m2 only has non-substantive failures
        substantive = self.ledger.get_substantive_failures("m2", "coding")
        self.assertEqual(len(substantive), 0)

    def test_get_failure_count(self):
        """Test getting failure counts."""
        count_all = self.ledger.get_failure_count("m1", "coding", only_substantive=False)
        count_substantive = self.ledger.get_failure_count("m1", "coding", only_substantive=True)

        # We have 2 coding entries (tests_failed, incorrect_code) + 1 review entry
        # So coding count should be 2
        self.assertEqual(count_all, 2)
        self.assertEqual(count_substantive, 2)

    def test_get_latest_entry(self):
        """Test getting the latest ledger entry."""
        latest = self.ledger.get_latest_entry("m1")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["model_id"], "m1")


class TestMultipleFailuresAndMixed(TestCase):
    """Tests for multiple failures accumulation."""

    def setUp(self):
        """Create test state and ledger with unique temp dir."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.ledger = FailureLedger(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multiple_failures_accumulate(self):
        """Test multiple failures accumulate correctly."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Record 3 substantive failures
        for i in range(3):
            registry = self.ledger.record_failure(
                model_id="m1",
                provider="p1",
                goal_id=f"g{i}",
                goal_class="coding",
                classification="incorrect_code",
                evidence=[f"evidence/{i}"],
                registry=registry,
            )

        # Check all entries in ledger
        entries = self.state.get_ledger_entries()
        self.assertEqual(len(entries), 3)

        # Check failure counts in each entry
        self.assertEqual(entries[0]["consecutive_substantive_failures"], 1)
        self.assertEqual(entries[1]["consecutive_substantive_failures"], 2)
        self.assertEqual(entries[2]["consecutive_substantive_failures"], 3)

        # Check model is quarantined
        from second_shift.registry import ModelRegistry
        model = ModelRegistry(self.state).get_model("m1", registry)
        self.assertEqual(model.get("status"), "unusable")

    def test_mixed_substantive_and_non_substantive(self):
        """Test mixed substantive and non-substantive failures."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Record mixed failures
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g1", goal_class="coding",
            classification="timeout", evidence=["e1"], registry=registry,
        )
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g2", goal_class="coding",
            classification="incorrect_code", evidence=["e2"], registry=registry,
        )
        self.ledger.record_failure(
            model_id="m1", provider="p1", goal_id="g3", goal_class="coding",
            classification="no_code", evidence=["e3"], registry=registry,
        )

        count = self.ledger.get_failure_count("m1", "coding", only_substantive=True)
        self.assertEqual(count, 1)  # Only incorrect_code counts


class TestValidation(TestCase):
    """Tests for ledger entry validation."""

    def setUp(self):
        """Create test state and ledger with unique temp dir."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.ledger = FailureLedger(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_valid_entry(self):
        """Test valid entry passes validation."""
        entry = {
            "model_id": "m1",
            "provider": "p1",
            "goal_id": "g1",
            "goal_class": "coding",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "classification": "tests_failed",
            "credit_status": "unknown",
            "counted_as_substantive_failure": True,
            "evidence": ["e1"],
        }

        errors = self.ledger.validate_entry(entry)
        self.assertEqual(errors, [])

    def test_missing_required_field(self):
        """Test missing required field is detected."""
        entry = {
            "model_id": "m1",
            # Missing provider
            "goal_id": "g1",
            "goal_class": "coding",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "classification": "tests_failed",
            "credit_status": "unknown",
            "counted_as_substantive_failure": True,
            "evidence": ["e1"],
        }

        errors = self.ledger.validate_entry(entry)
        self.assertIn("Missing required field: provider", errors)

    def test_invalid_classification(self):
        """Test invalid classification is detected."""
        entry = {
            "model_id": "m1",
            "provider": "p1",
            "goal_id": "g1",
            "goal_class": "coding",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "classification": "invalid_classification",
            "credit_status": "unknown",
            "counted_as_substantive_failure": True,
            "evidence": ["e1"],
        }

        errors = self.ledger.validate_entry(entry)
        self.assertIn("Invalid classification: invalid_classification", errors)

    def test_evidence_not_list(self):
        """Test evidence must be a list."""
        entry = {
            "model_id": "m1",
            "provider": "p1",
            "goal_id": "g1",
            "goal_class": "coding",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "classification": "tests_failed",
            "credit_status": "unknown",
            "counted_as_substantive_failure": True,
            "evidence": "not_a_list",
        }

        errors = self.ledger.validate_entry(entry)
        self.assertIn("evidence must be a list", errors)


if __name__ == "__main__":
    unittest_main()
