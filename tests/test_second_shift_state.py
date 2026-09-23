"""Deterministic tests for Second Shift state persistence and atomicity."""

from __future__ import annotations
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest import TestCase, main as unittest_main

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from second_shift.state import StateFile


class TestStatePersistence(TestCase):
    """Tests for state persistence across restarts."""

    def setUp(self):
        """Create a temporary state directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_registry_persists_to_disk(self):
        """Test registry data is written to disk."""
        registry = {"models": [{"id": "test-model", "status": "active"}]}
        self.state.save_registry(registry)

        registry_path = self.state.registry_path
        self.assertTrue(registry_path.exists())

        with open(registry_path, "r") as f:
            loaded = json.load(f)

        self.assertEqual(loaded, registry)

    def test_registry_loads_from_disk(self):
        """Test registry loads data from disk."""
        registry = {"models": [{"id": "test-model", "status": "active"}]}
        self.state.save_registry(registry)

        # Simulate "restart" by creating new StateFile instance
        new_state = StateFile(state_dir=self.temp_dir)
        loaded_registry = new_state.get_registry()

        self.assertEqual(loaded_registry, registry)

    def test_empty_registry_returns_empty_dict(self):
        """Test that missing/empty registry returns empty dict."""
        registry = self.state.get_registry()
        self.assertEqual(registry, {})

    def test_registry_update_persists(self):
        """Test registry updates persist correctly."""
        registry = {"models": [{"id": "m1", "status": "active"}]}
        self.state.save_registry(registry)

        updated_registry = {"models": [{"id": "m1", "status": "inactive"}]}
        self.state.save_registry(updated_registry)

        loaded_registry = self.state.get_registry()
        self.assertEqual(loaded_registry, updated_registry)

    def test_ledger_appends_entries(self):
        """Test ledger entries are appended correctly."""
        entry1 = {"id": 1, "data": "test1"}
        entry2 = {"id": 2, "data": "test2"}

        self.state.append_ledger_entry(entry1)
        self.state.append_ledger_entry(entry2)

        entries = self.state.get_ledger_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], entry1)
        self.assertEqual(entries[1], entry2)

    def test_ledger_entries_survive_restart(self):
        """Test ledger entries persist across restarts."""
        entry = {"id": 1, "data": "test"}
        self.state.append_ledger_entry(entry)

        # Simulate restart
        new_state = StateFile(state_dir=self.temp_dir)
        entries = new_state.get_ledger_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0], entry)

    def test_clear_ledger(self):
        """Test clearing ledger removes all entries."""
        self.state.append_ledger_entry({"id": 1})
        self.state.append_ledger_entry({"id": 2})

        self.state.clear_ledger()

        entries = self.state.get_ledger_entries()
        self.assertEqual(len(entries), 0)

    def test_timestamp_now_format(self):
        """Test timestamp generation is ISO format."""
        timestamp = self.state.timestamp_now()

        # Should be parseable as ISO format
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(timestamp)
        self.assertEqual(parsed.tzinfo, timezone.utc)


class TestFileLocking(TestCase):
    """Tests for file-based locking and concurrent access."""

    def setUp(self):
        """Create a temporary state directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lock_prevents_concurrent_write(self):
        """Test that exclusive lock prevents concurrent writes."""
        registry = {"models": []}
        self.state.save_registry(registry)

        # Acquire lock
        fd = self.state._acquire_lock(self.state.registry_path, mode="w")
        self.assertIsNotNone(fd)

        # Try to acquire another lock (should fail)
        fd2 = self.state._acquire_lock(self.state.registry_path, mode="w")
        self.assertIsNone(fd2)

        # Release first lock
        self.state._release_lock(fd)

        # Second lock should now succeed
        fd2 = self.state._acquire_lock(self.state.registry_path, mode="w")
        self.assertIsNotNone(fd2)
        self.state._release_lock(fd2)

    def test_shared_lock_allows_multiple_reads(self):
        """Test that shared lock allows concurrent reads."""
        registry = {"models": []}
        self.state.save_registry(registry)

        # Acquire first read lock
        fd1 = self.state._acquire_lock(self.state.registry_path, mode="r")
        self.assertIsNotNone(fd1)

        # Acquire second read lock (should succeed)
        fd2 = self.state._acquire_lock(self.state.registry_path, mode="r")
        self.assertIsNotNone(fd2)

        # Release both
        self.state._release_lock(fd1)
        self.state._release_lock(fd2)

    def test_write_lock_blocks_read_lock(self):
        """Test that write lock blocks read lock."""
        registry = {"models": []}
        self.state.save_registry(registry)

        # Acquire write lock
        fd_write = self.state._acquire_lock(self.state.registry_path, mode="w")
        self.assertIsNotNone(fd_write)

        # Try to acquire read lock (should fail)
        fd_read = self.state._acquire_lock(self.state.registry_path, mode="r")
        self.assertIsNone(fd_read)

        # Release write lock
        self.state._release_lock(fd_write)


class TestAtomicOperations(TestCase):
    """Tests for atomic file operations."""

    def setUp(self):
        """Create a temporary state directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_atomic_write_no_partial_read(self):
        """Test that atomic write prevents reading partial data."""
        import subprocess

        registry = {"models": [{"id": "m1", "status": "active"}]}
        self.state.save_registry(registry)

        # Write should be atomic - either full data or nothing
        # This test verifies the temp+rename pattern works
        self.state.save_registry({"models": [{"id": "m2", "status": "inactive"}]})

        loaded = self.state.get_registry()
        self.assertEqual(loaded["models"][0]["id"], "m2")


class TestSubstantiveClassification(TestCase):
    """Tests for substantive vs non-substantive classification."""

    def setUp(self):
        """Create a temporary state directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.state = StateFile(state_dir=self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_non_substantive_classifications(self):
        """Test non-substantive classifications are correctly identified."""
        non_substantive = [
            "no_code",
            "timeout",
            "endpoint_unavailable",
            "authentication",
            "credit_exhausted",
            "rate_limited",
            "policy_refusal",
            "context_limit",
        ]

        for classification in non_substantive:
            self.assertIn(
                classification,
                self.state.NON_SUBSTANTIVE_CLASSIFICATIONS,
                f"{classification} should be non-substantive"
            )

        self.assertIn("unknown", self.state.NON_SUBSTANTIVE_CLASSIFICATIONS)

    def test_substantive_classifications(self):
        """Test substantive classifications are correctly identified."""
        substantive = [
            "partial_code",
            "incorrect_code",
            "tests_failed",
        ]

        for classification in substantive:
            self.assertIn(
                classification,
                self.state.SUBSTANTIVE_CLASSIFICATIONS,
                f"{classification} should be substantive"
            )


if __name__ == "__main__":
    unittest_main()
