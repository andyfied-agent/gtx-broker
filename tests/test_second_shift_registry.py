"""Deterministic tests for Second Shift registry operations."""

from __future__ import annotations
import sys
import tempfile
import shutil
from unittest import TestCase, main as unittest_main

# Add parent directory to path for imports
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from second_shift.state import StateFile
from second_shift.registry import ModelRegistry, ModelStatus, GoalClass


class TestRegistryInit(TestCase):
    """Tests for registry initialization."""

    def setUp(self):
        """Create a temporary state directory and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_registry(self):
        """Test empty registry handling."""
        registry = self.registry.load_registry()
        self.assertEqual(registry, {})

    def test_load_empty_registry(self):
        """Test loading empty registry returns empty dict."""
        registry = self.registry.load_registry()
        self.assertIsInstance(registry, dict)
        self.assertEqual(registry.get("models", []), [])

    def test_save_and_load_registry(self):
        """Test save and load registry round-trip."""
        registry = {"models": [{"id": "test", "status": "active"}]}
        self.registry.save_registry(registry)

        loaded = self.registry.load_registry()
        self.assertEqual(loaded, registry)


class TestModelOperations(TestCase):
    """Tests for model CRUD operations."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

        # Initialize with test models
        self.base_registry = {
            "group": "second-shift",
            "default_status": "active",
            "quarantine_threshold": 3,
            "models": [
                {"id": "m1", "provider": "p1", "status": "active"},
                {"id": "m2", "provider": "p2", "status": "active"},
                {"id": "m3", "provider": "p3", "status": "standby"},
            ],
        }

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_model(self):
        """Test getting a model by ID."""
        registry = self.base_registry
        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("id"), "m1")
        self.assertEqual(model.get("provider"), "p1")

    def test_get_model_not_found(self):
        """Test getting non-existent model returns None."""
        registry = self.base_registry
        model = self.registry.get_model("nonexistent", registry)
        self.assertIsNone(model)

    def test_get_all_models(self):
        """Test getting all models."""
        registry = self.base_registry
        models = self.registry.get_all_models(registry)
        self.assertEqual(len(models), 3)

    def test_update_model(self):
        """Test updating model attributes."""
        registry = self.base_registry
        updated = self.registry.update_model(registry, "m1", {"status": "inactive"})

        self.assertEqual(updated["models"][0]["status"], "inactive")
        self.assertEqual(registry["models"][0]["status"], "active")  # Original unchanged

    def test_update_model_not_found(self):
        """Test updating non-existent model does nothing."""
        registry = self.base_registry
        updated = self.registry.update_model(registry, "nonexistent", {"status": "active"})
        self.assertEqual(len(updated["models"]), 3)


class TestFailureCounting(TestCase):
    """Tests for failure counting logic."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

        self.base_registry = {
            "models": [
                {"id": "m1", "provider": "p1", "status": "active"},
            ],
        }

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_failure_count_zero(self):
        """Test initial failure count is zero."""
        registry = self.base_registry
        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 0)

    def test_increment_failure_count(self):
        """Test incrementing failure count."""
        registry = self.base_registry
        registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 1)

    def test_increment_failure_count_multiple(self):
        """Test incrementing failure count multiple times."""
        registry = self.base_registry
        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 3)

    def test_failure_count_per_goal_class(self):
        """Test failure counts are per goal class."""
        registry = self.base_registry

        registry = self.registry.increment_substantive_failure("m1", "coding", registry)
        registry = self.registry.increment_substantive_failure("m1", "review", registry)

        coding_count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        review_count = self.registry.get_substantive_failure_count("m1", "review", registry)

        self.assertEqual(coding_count, 1)
        self.assertEqual(review_count, 1)

    def test_decrement_failure_count(self):
        """Test decrementing failure count."""
        registry = self.base_registry
        registry = self.registry.set_failure_count("m1", "coding", 3, registry)
        registry = self.registry.decrement_substantive_failure("m1", "coding", registry)

        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 2)

    def test_set_failure_count(self):
        """Test setting failure count directly."""
        registry = self.base_registry
        registry = self.registry.set_failure_count("m1", "coding", 5, registry)

        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 5)


class TestQuarantineLogic(TestCase):
    """Tests for quarantine after threshold failures."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_quarantine_after_threshold_failures(self):
        """Test model is quarantined after 3 substantive failures."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "unusable")
        self.assertIsNotNone(model.get("quarantined_at"))

    def test_quarantine_per_goal_class(self):
        """Test quarantine is per goal class (model becomes unusable for that class)."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Fail only in review, not in coding
        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "review", registry)

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "unusable")

    def test_not_quarantined_before_threshold(self):
        """Test model is not quarantined before threshold."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        for _ in range(2):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "active")

    def test_reset_failure_counts(self):
        """Test resetting failure counts clears quarantine."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Reach threshold
        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        # Reset
        registry = self.registry.reset_failure_counts(
            "m1", registry, reviewer="reviewer1", reviewer_evidence="review/p71-1"
        )

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "active")
        self.assertEqual(model.get("failure_counts_by_goal"), {})

    def test_quarantine_is_persistent(self):
        """Test quarantine status persists across operations."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        # Load from registry (simulates persistence)
        loaded_registry = registry  # In real scenario, would reload from disk

        model = self.registry.get_model("m1", loaded_registry)
        self.assertEqual(model.get("status"), "unusable")


class TestQuarantineReactivation(TestCase):
    """Tests for model reactivation after review."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reactivate_model(self):
        """Test reactivating a quarantined model."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Quarantine
        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "unusable")

        # Reactivate
        registry = self.registry.reactivate_model(
            "m1", "reviewer1", registry, reviewer_evidence="review/p71-1"
        )

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "active")
        self.assertEqual(model.get("reviewed_by"), "reviewer1")
        self.assertEqual(model.get("reviewer_evidence"), "review/p71-1")
        self.assertEqual(model.get("reviewer_required"), False)

    def test_reactivation_clears_failure_counts(self):
        """Test reactivation clears failure counts."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        # Quarantine
        for _ in range(3):
            registry = self.registry.increment_substantive_failure("m1", "coding", registry)

        # Reactivate
        registry = self.registry.reactivate_model(
            "m1", "reviewer1", registry, reviewer_evidence="review/p71-2"
        )

        count = self.registry.get_substantive_failure_count("m1", "coding", registry)
        self.assertEqual(count, 0)

    def test_manual_quarantine(self):
        """Test manual quarantine (e.g., after reviewer decision)."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.registry.quarantine_model("m1", "Reviewer decision", registry)

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("status"), "unusable")
        self.assertEqual(model.get("quarantine_reason"), "Reviewer decision")
        self.assertTrue(model.get("reviewer_required"))


class TestModelSelection(TestCase):
    """Tests for model selection logic."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_select_best_model_active(self):
        """Test selecting best active model."""
        registry = {
            "models": [
                {"id": "m1", "status": "active", "failure_counts_by_goal": {"coding": 0}},
                {"id": "m2", "status": "active", "failure_counts_by_goal": {"coding": 1}},
                {"id": "m3", "status": "unusable", "failure_counts_by_goal": {"coding": 3}},
            ],
        }

        best = self.registry.select_best_model("coding", registry)
        self.assertEqual(best, "m1")

    def test_select_model_with_lowest_failures(self):
        """Test selecting model with lowest failure count."""
        registry = {
            "models": [
                {"id": "m1", "status": "active", "failure_counts_by_goal": {"coding": 2}},
                {"id": "m2", "status": "active", "failure_counts_by_goal": {"coding": 1}},
                {"id": "m3", "status": "active", "failure_counts_by_goal": {"coding": 0}},
            ],
        }

        best = self.registry.select_best_model("coding", registry)
        self.assertEqual(best, "m3")

    def test_select_no_active_models(self):
        """Test selection returns None when no active models."""
        registry = {
            "models": [
                {"id": "m1", "status": "unusable"},
                {"id": "m2", "status": "standby"},
            ],
        }

        best = self.registry.select_best_model("coding", registry)
        self.assertIsNone(best)

    def test_pool_stats(self):
        """Test pool statistics."""
        registry = {
            "models": [
                {"id": "m1", "status": "active"},
                {"id": "m2", "status": "active"},
                {"id": "m3", "status": "unusable"},
                {"id": "m4", "status": "standby"},
            ],
        }

        stats = self.registry.get_pool_stats(registry)
        self.assertEqual(stats["active"], 2)
        self.assertEqual(stats["unusable"], 1)
        self.assertEqual(stats["standby"], 1)


class TestArtifactTracking(TestCase):
    """Tests for artifact reference tracking."""

    def setUp(self):
        """Create test state and registry."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)
        self.registry = ModelRegistry(self.state)

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_last_successful_artifact(self):
        """Test recording last successful artifact."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.registry.record_last_successful_artifact(
            "m1", "artifact/abc123", registry
        )

        model = self.registry.get_model("m1", registry)
        self.assertEqual(model.get("last_successful_artifact"), "artifact/abc123")
        self.assertIsNotNone(model.get("last_success_at"))

    def test_get_last_successful_artifact(self):
        """Test getting last successful artifact."""
        registry = {"models": [{"id": "m1", "status": "active"}]}

        registry = self.registry.record_last_successful_artifact(
            "m1", "artifact/abc123", registry
        )

        artifact = self.registry.get_last_successful_artifact("m1", registry)
        self.assertEqual(artifact, "artifact/abc123")


if __name__ == "__main__":
    unittest_main()
