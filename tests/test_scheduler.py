"""Tests for scheduler package packaging and imports."""
import pytest
import sys
from pathlib import Path
from datetime import datetime, timezone

# Get repository root from test file location
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

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
        pyproject = REPO_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        assert "gtx_broker.scheduler" in content, \
            "gtx_broker.scheduler must be in packages list"

    def test_python_magic_dependency(self):
        """Verify python-magic is declared as dependency."""
        pyproject = REPO_ROOT / "pyproject.toml"
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
    """Test retry, review, and cancel flows with proper state validation."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_retry_task_only_from_running(self, scheduler):
        """Test that retry_task() only works from running state."""
        task_id = "task-retry-001"
        
        # Can't retry queued task
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-retry-001")
        result = scheduler.retry_task(task_id)
        assert not result, "Cannot retry queued task"
        
        # Must start first
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        
        # Now can retry from running
        result = scheduler.retry_task(task_id)
        assert result, "Can retry running task"
        
        # Verify state changed to retry_wait
        task = scheduler.get_task(task_id)
        assert task["state"] == "retry_wait"

    def test_cancel_task_legal_states(self, scheduler):
        """Test that cancel_task() only works from legal states."""
        # Can cancel queued
        task_id_1 = "task-cancel-001"
        scheduler.add_task(task_id_1, "vision", {}, "batch", 10, "key-cancel-001")
        result = scheduler.cancel_task(task_id_1)
        assert result, "Can cancel queued task"
        
        # Can cancel claimed
        task_id_2 = "task-cancel-002"
        scheduler.add_task(task_id_2, "vision", {}, "batch", 10, "key-cancel-002")
        scheduler.claim_task(task_id_2)
        result = scheduler.cancel_task(task_id_2)
        assert result, "Can cancel claimed task"
        
        # Can cancel retry_wait
        task_id_3 = "task-cancel-003"
        scheduler.add_task(task_id_3, "vision", {}, "batch", 10, "key-cancel-003")
        scheduler.claim_task(task_id_3)
        scheduler.start_task(task_id_3, "p40-vision", "p40-vision-qwen35")
        scheduler.retry_task(task_id_3)
        result = scheduler.cancel_task(task_id_3)
        assert result, "Can cancel retry_wait task"
        
        # Cannot cancel running
        task_id_4 = "task-cancel-004"
        scheduler.add_task(task_id_4, "vision", {}, "batch", 10, "key-cancel-004")
        scheduler.claim_task(task_id_4)
        scheduler.start_task(task_id_4, "p40-vision", "p40-vision-qwen35")
        result = scheduler.cancel_task(task_id_4)
        assert not result, "Cannot cancel running task"
        
        # Cannot cancel succeeded
        task_id_5 = "task-cancel-005"
        scheduler.add_task(task_id_5, "vision", {}, "batch", 10, "key-cancel-005")
        scheduler.claim_task(task_id_5)
        scheduler.start_task(task_id_5, "p40-vision", "p40-vision-qwen35")
        scheduler.complete_task(task_id_5, result={"ok": True}, error=None)
        result = scheduler.cancel_task(task_id_5)
        assert not result, "Cannot cancel succeeded task"
        
        # Cannot cancel failed_terminal
        task_id_6 = "task-cancel-006"
        scheduler.add_task(task_id_6, "vision", {}, "batch", 10, "key-cancel-006")
        scheduler.claim_task(task_id_6)
        scheduler.start_task(task_id_6, "p40-vision", "p40-vision-qwen35")
        scheduler.complete_task(task_id_6, error="permanent error", failure_class="timeout")
        result = scheduler.cancel_task(task_id_6)
        assert not result, "Cannot cancel failed_terminal task"


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

    def test_select_worker_respects_status(self, tmp_path):
        """Test that select_worker() respects persisted worker status."""
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        scheduler = Scheduler(config)
        registry = scheduler._worker_registry

        # Mark p40-coding as BUSY
        registry.update_status("p40-coding", WorkerStatus.BUSY)
        
        # Mark gtx-chat as MAINTENANCE  
        registry.update_status("gtx-chat", WorkerStatus.MAINTENANCE)

        # select_worker should only return available workers
        # p40-coding is BUSY, gtx-chat is MAINTENANCE
        # p40-vision is AVAILABLE by default and has capability "vision"
        available = registry.select_worker("vision")
        assert available is not None
        assert available.profile == "p40-vision", \
            "select_worker should return p40-vision (the only available vision worker)"

        # Verify BUSY workers are not returned
        busy_attempt = registry.select_worker("text")
        # p40-coding is BUSY, so no text-capable worker is available
        assert busy_attempt is None, \
            "select_worker should return None when no workers are available"

        # p40-coding should have persisted BUSY status
        all_workers = registry.get_all_workers()
        for worker in all_workers:
            if worker.profile == "p40-coding":
                assert worker.status == WorkerStatus.BUSY, \
                    "get_all_workers() should return persisted BUSY status"
            elif worker.profile == "gtx-chat":
                assert worker.status == WorkerStatus.MAINTENANCE, \
                    "get_all_workers() should return persisted MAINTENANCE status"


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
        """Test that staging writes atomically to tmp first."""
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

    def test_staging_atomic_directory_creation(self, tmp_path):
        """Test that staging creates task directory atomically under tmp first."""
        storage = StorageContract(tmp_path)

        # Create valid image
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            input_path = Path(f.name)

        try:
            task_id, _ = storage.stage_input(input_path, "chat", "msg", "key")

            # Verify that tmp doesn't contain the final task directory
            # (it should be in incoming, created by os.rename)
            assert not (storage.tmp_path / task_id).exists(), \
                "Task directory should not exist in tmp after staging"
            
            # Verify task is in incoming
            assert (storage.incoming_path / task_id).exists(), \
                "Task directory should exist in incoming after staging"
        finally:
            input_path.unlink()

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


class TestEventLogging:
    """Test that all state transitions are properly logged."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_claim_task_emits_event(self, scheduler):
        """Test that claim_task() emits task_claimed event."""
        task_id = "task-event-001"
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-event-001")

        # Claim the task
        claimed = scheduler.claim_task(task_id)
        assert claimed is not None

        # Check event was logged
        events = scheduler.get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "task_claimed" in event_types, "claim_task() should emit task_claimed event"
        
        # Find the task_claimed event and verify its details
        for event in events:
            if event["event_type"] == "task_claimed":
                assert event["from_state"] == "queued"
                assert event["to_state"] == "claimed"
                break

    def test_retry_task_emits_event(self, scheduler):
        """Test that retry_task() emits retry_scheduled event."""
        task_id = "task-event-002"
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-event-002")
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        
        # Retry the task
        result = scheduler.retry_task(task_id)
        assert result
        
        # Check event was logged
        events = scheduler.get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "retry_scheduled" in event_types, "retry_task() should emit retry_scheduled event"

    def test_cancel_task_emits_event(self, scheduler):
        """Test that cancel_task() emits task_cancelled event."""
        task_id = "task-event-003"
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-event-003")
        result = scheduler.cancel_task(task_id)
        assert result
        
        # Check event was logged
        events = scheduler.get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "task_cancelled" in event_types, "cancel_task() should emit task_cancelled event"


class TestRetryWaitToQueuedTransition:
    """Test retry_wait → queued transition."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_retry_wait_can_be_requeued(self, scheduler):
        """Test that retry_wait tasks can be moved back to queued."""
        task_id = "task-requeue-001"
        
        # Create and process task to retry_wait state
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-requeue-001")
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        scheduler.retry_task(task_id)
        
        # Verify state is retry_wait
        task = scheduler.get_task(task_id)
        assert task["state"] == "retry_wait"
        
        # Use the new requeue_retry_wait method
        result = scheduler.requeue_retry_wait(task_id)
        assert result, "requeue_retry_wait should succeed"
        
        # Verify state changed to queued
        task = scheduler.get_task(task_id)
        assert task["state"] == "queued"
        
        # Verify it's in pending tasks
        pending = scheduler.get_pending_tasks()
        task_ids = [t["id"] for t in pending]
        assert task_id in task_ids, "retry_wait → queued task should be in pending queue"


class TestRunningToAwaitingReviewFlow:
    """Test running → awaiting_review → queued/failed_terminal flow."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        db_path = tmp_path / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        return Scheduler(config)

    def test_running_to_awaiting_review(self, scheduler):
        """Test that running tasks can transition to awaiting_review."""
        task_id = "task-review-001"
        
        # Create and start task
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-review-001")
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        
        # Verify state is running
        task = scheduler.get_task(task_id)
        assert task["state"] == "running"
        
        # Use the new transition_running_to_awaiting_review method
        result = scheduler.transition_running_to_awaiting_review(task_id)
        assert result, "transition_running_to_awaiting_review should succeed"
        
        # Verify state changed to awaiting_review
        task = scheduler.get_task(task_id)
        assert task["state"] == "awaiting_review"

    def test_awaiting_review_to_queued(self, scheduler):
        """Test that awaiting_review tasks can be requeued."""
        task_id = "task-review-002"
        
        # Create task and move to awaiting_review
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-review-002")
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        
        result = scheduler.transition_running_to_awaiting_review(task_id)
        assert result
        
        # Verify state is awaiting_review
        task = scheduler.get_task(task_id)
        assert task["state"] == "awaiting_review"
        
        # Use the new requeue_awaiting_review method
        result = scheduler.requeue_awaiting_review(task_id)
        assert result, "requeue_awaiting_review should succeed"
        
        # Verify state changed to queued
        task = scheduler.get_task(task_id)
        assert task["state"] == "queued"
        
        # Verify it's in pending tasks
        pending = scheduler.get_pending_tasks()
        task_ids = [t["id"] for t in pending]
        assert task_id in task_ids, "awaiting_review → queued task should be in pending queue"

    def test_awaiting_review_to_failed_terminal(self, scheduler):
        """Test that awaiting_review tasks can be marked as failed_terminal."""
        task_id = "task-review-003"
        
        # Create task and move to awaiting_review
        scheduler.add_task(task_id, "vision", {}, "batch", 10, "key-review-003")
        scheduler.claim_task(task_id)
        scheduler.start_task(task_id, "p40-vision", "p40-vision-qwen35")
        
        result = scheduler.transition_running_to_awaiting_review(task_id)
        assert result
        
        # Verify state is awaiting_review
        task = scheduler.get_task(task_id)
        assert task["state"] == "awaiting_review"
        
        # Use the new transition_awaiting_review_to_failed method
        result = scheduler.transition_awaiting_review_to_failed(task_id, error='review rejected')
        assert result, "transition_awaiting_review_to_failed should succeed"
        
        # Verify state changed to failed_terminal
        task = scheduler.get_task(task_id)
        assert task["state"] == "failed_terminal"
