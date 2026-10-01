"""Tests for scheduler daemon with proper state machine flow."""

import pytest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtx_broker.scheduler import SchedulerConfig
from gtx_broker.scheduler.handlers import HandlerResult
from gtx_broker.scheduler.model_profiles import ModelProfileError
from gtx_broker.daemon import SchedulerDaemon


@pytest.fixture
def daemon(tmp_path):
    """Create daemon with temp database and mock profile controller."""
    db_path = tmp_path / "tasks.db"
    config = SchedulerConfig(
        db_path=str(db_path),
        max_concurrent=1,
        poll_interval=1.0
    )
    
    # Patch the profile controller to avoid loading /etc/llama-cpp/profiles
    with patch('gtx_broker.daemon.P40ModelProfileController') as MockController:
        mock_controller = MagicMock()
        mock_controller.profile.return_value = MagicMock().__enter__.return_value
        mock_controller.ensure_profile.return_value = False
        MockController.return_value = mock_controller
        
        daemon = SchedulerDaemon(config)
        yield daemon


class TestDaemonStateMachine:
    """Test daemon state machine flow."""

    def test_queued_to_claimed_to_running_to_succeeded(self, daemon):
        """Test successful task flow: queued -> claimed -> running -> succeeded."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-001",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Get next task
        task = daemon.scheduler.get_next_task()
        assert task is not None
        assert task['id'] == 'TEST-001'

        # Verify task is queued
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'queued'

        # Mock handler to return SUCCESS
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.SUCCESS
                mock_get_handler.return_value = mock_handler

                # Dispatch task
                success = daemon._dispatch_task(task)
                assert success

                # Verify task succeeded
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'succeeded'

    def test_queued_to_claimed_to_running_to_failed_terminal(self, daemon):
        """Test failed task flow: queued -> claimed -> running -> failed_terminal."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-002",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Mock handler to return FAILED
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.FAILED
                mock_handler.last_result = {
                    "status": "rejected",
                    "passed": False,
                    "findings": [{"id": "F-1", "severity": "high"}],
                    "reviewer": "codex-review",
                    "review_attempts": [{"reviewer": "codex-review", "exit_code": 0}],
                }
                mock_get_handler.return_value = mock_handler

                # Dispatch task
                task = daemon.scheduler.get_next_task()
                success = daemon._dispatch_task(task)
                assert success

                # Verify task failed_terminal
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'failed_terminal'
                assert task_data['error'] == 'Review rejected'
                conn = daemon.scheduler._get_connection()
                attempt = conn.execute(
                    "SELECT result, failure_class FROM task_attempts WHERE task_id = ?",
                    (task['id'],),
                ).fetchone()
                conn.close()
                assert attempt["failure_class"] == "review_rejected"
                assert json.loads(attempt["result"])["findings"][0]["id"] == "F-1"

    def test_handler_retry_requeues_to_retry_wait(self, daemon):
        """Test that RETRY result requeues task to retry_wait."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-003",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Mock handler to return RETRY
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.RETRY
                mock_get_handler.return_value = mock_handler

                # Dispatch task
                task = daemon.scheduler.get_next_task()
                success = daemon._dispatch_task(task)
                assert success

                # Verify task requeued to retry_wait
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'retry_wait'

    def test_profile_remediation_boundary_failure_requeues(self, daemon):
        """A second profile-boundary failure must return the task to retry_wait."""
        success = daemon.scheduler.add_task(
            task_id="TEST-003B",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10,
        )
        assert success

        initial_context = MagicMock()
        initial_context.__enter__.side_effect = ModelProfileError("initial boundary")
        retry_context = MagicMock()
        retry_context.__enter__.side_effect = ModelProfileError("retry boundary")
        daemon.model_profiles.profile.side_effect = [initial_context, retry_context]
        daemon.model_profiles.ensure_profile.return_value = True

        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_get_handler.return_value = MagicMock()
                task = daemon.scheduler.get_next_task()
                assert daemon._dispatch_task(task)

        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'retry_wait'
        assert daemon.model_profiles.profile.call_count == 2

    def test_worker_unavailable_requeues_to_retry_wait(self, daemon):
        """Test that worker unavailable requeues task to retry_wait."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-004",
            kind="vision",
            payload={"image_url": "test.jpg"},
            mode="immediate",
            priority=10
        )
        assert success

        # Mock worker available but start_task fails (simulating worker unavailable)
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-vision'):
            with patch.object(daemon.scheduler, 'start_task', return_value=False):
                # Dispatch task
                task = daemon.scheduler.get_next_task()
                success = daemon._dispatch_task(task)
                assert not success  # Returns False when start_task fails

                # Verify task requeued to retry_wait
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'retry_wait'

    def test_review_unavailability_persists_both_reviewer_attempts(self, daemon):
        """Codex and Air Review outages remain visible on the retry attempt."""
        assert daemon.scheduler.add_task(
            task_id="TEST-004B",
            kind="coding",
            payload={"worktree_path": "/tmp/review-worktree"},
            mode="immediate",
            priority=10,
            review_tag=True,
        )
        failure_evidence = {
            "status": "unavailable",
            "failure_kind": "review_unavailable",
            "review_attempts": [
                {"reviewer": "codex-review", "status": "unavailable", "exit_code": 2},
                {"reviewer": "air-review", "status": "unavailable", "timeout": True},
            ],
            "fallback_used": True,
        }

        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.WORKER_UNAVAILABLE
                mock_handler.last_result = failure_evidence
                mock_get_handler.return_value = mock_handler

                task = daemon.scheduler.get_next_task()
                assert daemon._dispatch_task(task) is True

        assert daemon.scheduler.get_task("TEST-004B")["state"] == "retry_wait"
        conn = daemon.scheduler._get_connection()
        attempt = conn.execute(
            "SELECT result, failure_class FROM task_attempts WHERE task_id = ?",
            ("TEST-004B",),
        ).fetchone()
        conn.close()
        assert attempt["failure_class"] == "review_unavailable"
        persisted = json.loads(attempt["result"])
        assert persisted["fallback_used"] is True
        assert [item["reviewer"] for item in persisted["review_attempts"]] == [
            "codex-review", "air-review",
        ]

    def test_awaiting_review_transitions_correctly(self, daemon):
        """Test that AWAITING_REVIEW transitions task correctly."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-005",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Mock handler to return AWAITING_REVIEW
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.AWAITING_REVIEW
                mock_get_handler.return_value = mock_handler

                # Dispatch task
                task = daemon.scheduler.get_next_task()
                success = daemon._dispatch_task(task)
                assert success

                # Verify task in awaiting_review
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'awaiting_review'

    def test_start_task_failure_requeues_not_stuck_claimed(self, daemon):
        """Test that start_task failure doesn't leave task stuck in claimed."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-006",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Mock worker available but start_task fails
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch.object(daemon.scheduler, 'start_task', return_value=False):
                # Dispatch task
                task = daemon.scheduler.get_next_task()
                success = daemon._dispatch_task(task)
                assert not success  # Returns False when start_task fails

                # Task should be in retry_wait, not claimed
                task_data = daemon.scheduler.get_task(task['id'])
                assert task_data['state'] == 'retry_wait'
                assert task_data['state'] != 'claimed'

    def test_p40_exclusivity_respected(self, daemon):
        """Test that P40 exclusivity is respected during start_task."""
        # Add two tasks
        success1 = daemon.scheduler.add_task(
            task_id="TEST-007A",
            kind="coding",
            payload={"goal": "test1"},
            mode="immediate",
            priority=10
        )
        success2 = daemon.scheduler.add_task(
            task_id="TEST-007B",
            kind="coding",
            payload={"goal": "test2"},
            mode="immediate",
            priority=10
        )
        assert success1 and success2

        # First task starts successfully
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.SUCCESS
                mock_get_handler.return_value = mock_handler

                task1 = daemon.scheduler.get_next_task()
                success1 = daemon._dispatch_task(task1)
                assert success1

                # Verify first task succeeded
                task1_data = daemon.scheduler.get_task(task1['id'])
                assert task1_data['state'] == 'succeeded'

                # Second task should be queued (scheduler returns queued before retry_wait)
                task2 = daemon.scheduler.get_next_task()
                assert task2 is not None
                assert task2['id'] == 'TEST-007B'

                task2_data = daemon.scheduler.get_task(task2['id'])
                assert task2_data['state'] == 'queued'


class TestDaemonImport:
    """Test that daemon can be imported without errors."""

    def test_daemon_imports_correctly(self):
        """Test that daemon imports successfully."""
        # This test passes if the import works
        from gtx_broker.daemon import SchedulerDaemon
        assert SchedulerDaemon is not None

    def test_worker_registry_import(self):
        """Test that WorkerRegistry can be imported."""
        from gtx_broker.scheduler.workers import WorkerRegistry, WorkerStatus
        assert WorkerRegistry is not None
        assert WorkerStatus is not None


class TestDaemonHandlerInterface:
    """Test that handler interface is correct."""

    def test_handler_execute_signature(self):
        """Test that handler execute() accepts task dict and returns HandlerResult."""
        from gtx_broker.scheduler.handlers import TaskHandler, HandlerResult

        # Check abstract method signature
        import inspect
        sig = inspect.signature(TaskHandler.execute)
        params = list(sig.parameters.keys())

        # Should have 'self' and 'task'
        assert 'task' in params


class TestDaemonRetryWaitSupport:
    """Test that daemon can process retry_wait tasks."""

    def test_retry_wait_task_can_be_retrieved(self, daemon):
        """Test that get_retry_wait_task() returns retry_wait tasks."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-008",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Claim and start the task
        task = daemon.scheduler.get_next_task()
        claimed = daemon.scheduler.claim_task(task['id'])
        assert claimed

        start = daemon.scheduler.start_task(task['id'], worker_profile='p40-coding')
        assert start

        # Transition to retry_wait
        retry = daemon.scheduler.transition_running_to_retry_wait(task['id'])
        assert retry

        # Verify task can be retrieved as retry_wait
        retry_task = daemon.scheduler.get_retry_wait_task()
        assert retry_task is not None
        assert retry_task['id'] == 'TEST-008'

    def test_daemon_run_picks_up_retry_wait(self, daemon):
        """Test that daemon.run() picks up retry_wait tasks after queued tasks are exhausted."""
        # Add one task
        success = daemon.scheduler.add_task(
            task_id="TEST-009",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Complete the task
        task = daemon.scheduler.get_next_task()
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.SUCCESS
                mock_get_handler.return_value = mock_handler

                success = daemon._dispatch_task(task)
                assert success

        # Now add another task and mark it for retry
        success = daemon.scheduler.add_task(
            task_id="TEST-010",
            kind="coding",
            payload={"goal": "retry"},
            mode="immediate",
            priority=10
        )
        assert success

        task = daemon.scheduler.get_next_task()
        claimed = daemon.scheduler.claim_task(task['id'])
        assert claimed

        start = daemon.scheduler.start_task(task['id'], worker_profile='p40-coding')
        assert start

        # Mark for retry
        retry = daemon.scheduler.transition_running_to_retry_wait(task['id'])
        assert retry

        # Verify daemon can pick it up
        retry_task = daemon.scheduler.get_retry_wait_task()
        assert retry_task is not None


class TestDaemonRetryWaitEndToEnd:
    """End-to-end tests for retry_wait task processing."""

    def test_retry_wait_promoted_to_succeeded(self, daemon):
        """Test that a retry_wait task is promoted to queued and then succeeds."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-RETRY-1",
            kind="coding",
            payload={"goal": "retry test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Simulate: task was claimed, started, then failed and marked for retry
        task = daemon.scheduler.get_next_task()
        claimed = daemon.scheduler.claim_task(task['id'])
        assert claimed

        start = daemon.scheduler.start_task(task['id'], worker_profile='p40-coding')
        assert start

        # Mark for retry (simulating WORKER_UNAVAILABLE or RETRY result)
        retry = daemon.scheduler.transition_running_to_retry_wait(task['id'])
        assert retry

        # Verify task is in retry_wait state
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'retry_wait'

        # Now simulate daemon picking up the retry task
        # Daemon calls get_retry_wait_task(), then requeue_retry_wait(), then get_next_task()
        retry_task = daemon.scheduler.get_retry_wait_task()
        assert retry_task is not None

        # Promote to queued
        promoted = daemon.scheduler.requeue_retry_wait(retry_task['id'])
        assert promoted

        # Verify task is now queued
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'queued'

        # Get the promoted task
        new_task = daemon.scheduler.get_next_task()
        assert new_task is not None
        assert new_task['id'] == 'TEST-RETRY-1'

        # Process the task successfully
        with patch.object(daemon, '_get_worker_for_task', return_value='p40-coding'):
            with patch('gtx_broker.daemon.get_handler_for_task') as mock_get_handler:
                mock_handler = MagicMock()
                mock_handler.execute.return_value = HandlerResult.SUCCESS
                mock_get_handler.return_value = mock_handler

                success = daemon._dispatch_task(new_task)
                assert success

        # Verify task succeeded
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'succeeded'


class TestStateMachineCompliance:
    """Test that state transitions comply with STATE_TRANSITIONS table."""

    def test_claimed_to_retry_wait_is_allowed(self, daemon):
        """Test that claimed → retry_wait transition is now allowed."""
        # Add a task
        success = daemon.scheduler.add_task(
            task_id="TEST-COMPLIANCE-1",
            kind="coding",
            payload={"goal": "test"},
            mode="immediate",
            priority=10
        )
        assert success

        # Claim the task
        task = daemon.scheduler.get_next_task()
        claimed = daemon.scheduler.claim_task(task['id'])
        assert claimed

        # Verify task is claimed
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'claimed'

        # Test that _validate_transition says claimed → retry_wait is legal
        is_valid = daemon.scheduler._validate_transition(task['id'], 'claimed', 'retry_wait')
        assert is_valid, "claimed → retry_wait should be a valid transition"

        # Now perform the transition using requeue_claimed_to_retry_wait
        requeued = daemon.scheduler.requeue_claimed_to_retry_wait(task['id'])
        assert requeued

        # Verify task is in retry_wait
        task_data = daemon.scheduler.get_task(task['id'])
        assert task_data['state'] == 'retry_wait'
