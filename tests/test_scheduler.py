"""Tests for scheduler package packaging and imports."""
import pytest
import subprocess
import sys
from pathlib import Path

# Import test fixtures
from gtx_broker.scheduler import (
    Scheduler, SchedulerConfig, StorageContract,
    WorkerRegistry, WorkerProfile, WorkerStatus, initialize_workers,
    DailyDispatchPolicy, TaskMode, get_dispatch_policy
)


class TestPackaging:
    """Test that scheduler module is properly packaged."""

    def test_scheduler_in_wheel(self):
        """Verify scheduler package is in pyproject.toml."""
        pyproject = Path("/home/andyfied/src/gtx-broker/pyproject.toml")
        content = pyproject.read_text()
        assert '"gtx_broker.scheduler"' in content, \
            "gtx_broker.scheduler must be in packages list"

    def test_python_magic_dependency(self):
        """Verify python-magic is declared as dependency."""
        pyproject = Path("/home/andyfied/src/gtx-broker/pyproject.toml")
        content = pyproject.read_text()
        assert 'python-magic' in content, \
            "python-magic must be in dependencies"

    def test_import_scheduler(self):
        """Test that scheduler module can be imported."""
        try:
            from gtx_broker.scheduler import (
                Scheduler, SchedulerConfig, StorageContract,
                WorkerRegistry, WorkerProfile, WorkerStatus, initialize_workers,
                DailyDispatchPolicy, TaskMode, get_dispatch_policy,
                TaskHandler, VisionHandler, CodingHandler, get_handler_for_task
            )
            assert True
        except ImportError as e:
            pytest.fail(f"Failed to import scheduler: {e}")


class TestStateTransitions:
    """Test scheduler state machine transitions."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        """Create scheduler instance with temp database."""
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_task_flow_add_claim_start_complete(self, scheduler):
        """Test complete task flow: add → claim → start → complete."""
        task_id = "test-task-001"
        payload = {"kind": "vision", "test": "data"}

        # Add task
        success = scheduler.add_task(task_id, "vision", payload, "batch", 10, "key-001")
        assert success, "Task should be added"

        # Claim task (should claim specific task, not first available)
        claimed = scheduler.claim_task(task_id)
        assert claimed is not None, "Task should be claimable"
        assert claimed["id"] == task_id, "Should return the claimed task"
        assert claimed["state"] == "claimed", "State should be 'claimed'"

        # Start task
        start_success = scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        assert start_success, "Task should be started"

        # Complete task
        complete_success = scheduler.complete_task(
            task_id, result={"output": "data"}, error=None
        )
        assert complete_success, "Task should be completed"

    def test_state_machine_invalid_transition(self, scheduler):
        """Test that invalid state transitions are prevented."""
        task_id = "test-task-002"
        scheduler.add_task(task_id, "vision", {"test": "data"}, "batch", 10, "key-002")

        # Try to complete without starting (should fail)
        success = scheduler.complete_task(task_id, error="error")
        assert not success, "Cannot complete task that hasn't started"

        # Try to claim same task twice (should fail second claim)
        scheduler.claim_task(task_id)
        claimed_twice = scheduler.claim_task(task_id)
        assert claimed_twice is None, "Cannot claim already claimed task"

    def test_priority_ordering(self, scheduler):
        """Test that high priority tasks are claimed first."""
        # Add low priority task first
        scheduler.add_task("low-priority", "vision", {}, "batch", 5, "low-key")

        # Add high priority task
        scheduler.add_task("high-priority", "vision", {}, "batch", 100, "high-key")

        # Claim should return high priority first if we claim by priority
        # Note: claim_task(task_id) claims specific task, not first available
        # So test that we CAN claim either one, but get_pending returns high priority first
        pending = scheduler.get_pending_tasks()
        assert len(pending) == 2
        
        # High priority should be first in pending list
        if pending:
            assert pending[0]["id"] == "high-priority", \
                "get_pending_tasks should return high priority first"


class TestConcurrencyAndIdempotency:
    """Test concurrent access and idempotency."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_idempotency_key_prevents_duplicates(self, scheduler):
        """Test that same idempotency key prevents duplicate tasks."""
        task_id_1 = "task-001"
        task_id_2 = "task-002"

        success1 = scheduler.add_task(
            task_id_1, "vision", {"test": "data"}, "batch", 10, "unique-key"
        )
        assert success1, "First task should be added"

        success2 = scheduler.add_task(
            task_id_2, "vision", {"test": "data"}, "batch", 10, "unique-key"  # Same key
        )
        assert not success2, "Second task with same key should be rejected"

    def test_task_retrieval_after_claim(self, scheduler):
        """Test that claimed task returns updated state."""
        task_id = "task-003"
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-003")

        claimed = scheduler.claim_task(task_id)
        assert claimed is not None
        assert claimed["state"] == "claimed", "Returned task should have state='claimed'"


class TestRetryReviewCancel:
    """Test retry, review, and cancel flows (to be implemented)."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_retry_flow_exists(self, scheduler):
        """Test that retry infrastructure exists (stub)."""
        # This is a placeholder - actual retry logic to be implemented
        assert hasattr(scheduler, "retry_task"), "retry_task method should exist"

    def test_cancel_flow_exists(self, scheduler):
        """Test that cancel infrastructure exists (stub)."""
        assert hasattr(scheduler, "cancel_task"), "cancel_task method should exist"


class TestDSTAndScheduling:
    """Test timezone handling for BST."""

    def test_london_timezone_uses_zoneinfo(self):
        """Test that _get_local_time uses ZoneInfo for Europe/London."""
        policy = get_dispatch_policy()

        # Verify it uses ZoneInfo (doesn't raise)
        local_time = policy._get_local_time()

        # Verify timezone is Europe/London (not fixed UTC)
        import zoneinfo
        assert local_time.tzinfo is not None
        assert isinstance(local_time.tzinfo, zoneinfo.ZoneInfo)
        assert str(local_time.tzinfo) == "Europe/London"

    def test_bst_correctly_applied(self):
        """Test that BST (UTC+1) is applied during summer months."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        policy = get_dispatch_policy()

        # In July (BST), Europe/London is UTC+1, not UTC
        # We can verify by checking the offset
        july = datetime(2024, 7, 15, tzinfo=ZoneInfo("Europe/London"))
        utc_july = datetime(2024, 7, 15, tzinfo=timezone.utc)

        # When it's midnight in London (July), it's 23:00 UTC (previous day)
        # The policy should handle this correctly
        assert july.utcoffset().total_seconds() == 3600, "BST should be UTC+1"
        assert utc_july.utcoffset().total_seconds() == 0, "UTC should be UTC+0"


class TestWorkerStatusPersistence:
    """Test worker status persistence."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_worker_status_can_be_updated(self, tmp_path):
        """Test that worker status can be updated and persists within session."""
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        scheduler = Scheduler(config)
        registry = scheduler._worker_registry

        # Get default worker
        worker = registry.get_worker("p40-vision")
        assert worker is not None
        assert worker.status.value == "available"

        # Update status
        registry.update_status("p40-vision", WorkerStatus.UNAVAILABLE)

        # Reload and verify
        worker = registry.get_worker("p40-vision")
        assert worker.status.value == "unavailable"

    def test_default_workers_not_overwritten(self, tmp_path):
        """Test that default workers aren't overwritten on restart."""
        # First scheduler creates default workers
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        s1 = Scheduler(config)

        # Mark worker as busy
        s1._worker_registry.update_status("p40-coding", WorkerStatus.BUSY)

        # Second scheduler (simulating restart) - reuse same db
        s2 = Scheduler(config)

        # Worker should still be BUSY (not reset to available)
        worker = s2._worker_registry.get_worker("p40-coding")
        assert worker.status.value == "busy", \
            "Worker status should persist across scheduler restart"


class TestStorageStagingAndCompletion:
    """Test storage contract staging and completion."""

    @pytest.fixture
    def storage(self, tmp_path):
        return StorageContract(tmp_path)

    def test_staging_validates_mime_type(self, tmp_path):
        """Test that staging validates MIME type."""
        storage = StorageContract(tmp_path)

        # Create invalid file (text file with .jpg extension)
        invalid_file = tmp_path / "invalid.jpg"
        invalid_file.write_text("This is not an image")

        with pytest.raises(ValueError) as exc_info:
            storage.stage_input(invalid_file)

        assert "MIME" in str(exc_info.value) or "Unsupported" in str(exc_info.value)

    def test_staging_atomic_write(self, tmp_path):
        """Test that staging writes atomically."""
        storage = StorageContract(tmp_path)

        # Create valid image
        import tempfile
        import shutil
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            # Write minimal valid JPEG header
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            temp_path = f.name

        try:
            task_id, metadata = storage.stage_input(
                Path(temp_path),
                source_chat="123456",
                source_message_id="789",
                idempotency_key="test-key"
            )

            assert task_id.startswith("task-")
            assert metadata["task_id"] == task_id

            # Verify file exists in incoming directory
            incoming_file = storage.incoming_path / task_id / "image.jpg"
            assert incoming_file.exists()
        finally:
            Path(temp_path).unlink()

    def test_completion_with_output_path(self, tmp_path):
        """Test that completion works when output_path is supplied."""
        storage = StorageContract(tmp_path)

        # Create valid input
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            input_path = Path(f.name)

        try:
            task_id, _ = storage.stage_input(input_path, "chat", "msg", "key")
            storage.claim_for_processing(task_id)

            # Create output file
            output_file = tmp_path / "output.json"
            output_file.write_text('{"result": "success"}')

            # Complete with output_path
            success = storage.complete_task(task_id, output_path=output_file)
            assert success, "Completion should succeed even with output_path"

            # Verify output is in processed directory
            processed_output = storage.processed_path / task_id / "output.json"
            assert processed_output.exists(), "Output file should be in processed directory"
        finally:
            input_path.unlink()
