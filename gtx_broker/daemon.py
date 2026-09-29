"""Scheduler daemon: dispatch tasks to handlers with proper state machine flow."""

import logging
from contextlib import nullcontext
import os
import signal
import time
from pathlib import Path
from threading import Thread
from typing import Any, Dict, Optional

from gtx_broker.scheduler import Scheduler, SchedulerConfig
from gtx_broker.scheduler.handlers import HandlerResult, get_handler_for_task
from gtx_broker.scheduler.model_profiles import ModelProfileError, P40ModelProfileController
from gtx_broker.status_api import StatusAPI

logger = logging.getLogger(__name__)


class SchedulerDaemon:
    """Scheduler daemon with proper state machine flow.

    Flow:
    1. claim_task() - atomically claim the task (queued→claimed)
    2. start_task() - atomically start with P40 locking (claimed→running)
    3. Execute handler (blocking call)
    4. Transition to final state (running→succeeded/failed/retry_wait/awaiting_review)
    """

    def __init__(self, config: SchedulerConfig):
        """Initialize daemon.

        Args:
            config: Scheduler config
        """
        self.config = config
        self.scheduler = Scheduler(config)
        self.storage = self.scheduler._storage
        self.model_profiles = P40ModelProfileController()
        self.retention_days = self._configured_retention_days()
        self._active_tasks: Dict[str, bool] = {}
        self._running = False
        self._api: Optional[StatusAPI] = None
        self._api_thread: Optional[Thread] = None
        self._setup_signals()
        self._recover_interrupted_tasks()
        self._run_retention_cleanup()
        self._last_retention_cleanup = time.monotonic()

        logger.info("Scheduler daemon initialized")

    def _start_status_api(self, port: int = 11439) -> bool:
        """Start status API server.

        Args:
            port: Port to listen on (default 11439)

        Returns:
            True if server started successfully
        """
        self._api = StatusAPI(self.scheduler, port)
        if not self._api.start():
            logger.warning("Failed to start status API on port %d", port)
            return False

        # Start background thread to run server
        self._api_thread = Thread(
            target=self._api.run_forever,
            daemon=True,
            name="gtx-status-api",
        )
        self._api_thread.start()
        logger.info("Status API server started on port %d", port)
        return True

    @staticmethod
    def _configured_retention_days() -> int:
        raw = os.getenv("GTX_IMAGE_RETENTION_DAYS", "30")
        try:
            days = int(raw)
        except ValueError:
            logger.warning("Invalid GTX_IMAGE_RETENTION_DAYS=%r; using 30", raw)
            return 30
        if days < 0:
            logger.warning("Negative GTX_IMAGE_RETENTION_DAYS=%r; using 30", raw)
            return 30
        return days

    @staticmethod
    def _configured_status_api_port() -> int:
        raw = os.getenv("GTX_STATUS_API_PORT", "11439")
        try:
            port = int(raw)
        except ValueError:
            logger.warning("Invalid GTX_STATUS_API_PORT=%r; using 11439", raw)
            return 11439
        if not 1 <= port <= 65535:
            logger.warning("GTX_STATUS_API_PORT=%r is outside 1-65535; using 11439", raw)
            return 11439
        return port

    def _run_retention_cleanup(self) -> None:
        try:
            removed = self.storage.cleanup_old_tasks(self.retention_days)
            if removed:
                logger.info("Removed %s processed image task(s) past retention", removed)
        except (OSError, ValueError) as exc:
            logger.warning("Image retention cleanup failed: %s", exc)

    def _recover_interrupted_tasks(self) -> None:
        """Reconcile scheduler and durable storage state after a restart."""
        for task_id in self.storage.task_ids():
            task = self.scheduler.get_task(task_id)
            if task is None:
                logger.error("Leaving untracked durable input %s for manual recovery", task_id)
                continue

            state = task.get("state")
            metadata = self.storage.get_task(task_id) or {}
            storage_status = metadata.get("status")
            outcome = metadata.get("scheduler_outcome")

            if state in {"running", "claimed"} and outcome in {
                "succeeded", "failed", "awaiting_review", "retry_wait"
            }:
                if outcome == "retry_wait":
                    storage_ready = storage_status == "accepted" or self.storage.requeue_for_retry(task_id)
                    recovered = storage_ready and self._transition_to_retry(task_id, state)
                elif storage_status != "processed":
                    storage_ready = self.storage.complete_task(
                        task_id, result=metadata.get("result"), outcome=outcome
                    )
                    recovered = storage_ready and self._finish_recovered_task(task_id, state, outcome, metadata)
                else:
                    recovered = self._finish_recovered_task(task_id, state, outcome, metadata)
                if not recovered:
                    logger.error("Could not reconcile image task %s after restart", task_id)
                continue

            if state in {"running", "claimed"}:
                recovered = self._transition_to_retry(task_id, state)
                if recovered and storage_status != "accepted":
                    recovered = self.storage.requeue_for_retry(task_id)
                if recovered:
                    self.storage.update_metadata(task_id, {
                        "recovered_after_restart": True,
                        "recovery_state": state,
                    })
                    logger.info("Requeued interrupted image task %s from %s", task_id, state)
                else:
                    logger.error("Could not requeue interrupted image task %s", task_id)
            elif state == "retry_wait" and storage_status == "processing":
                if not self.storage.requeue_for_retry(task_id):
                    logger.error("Could not requeue retrying image task %s", task_id)
            elif state in {"succeeded", "failed_terminal", "awaiting_review", "cancelled"}:
                if storage_status == "processing" and not self.storage.complete_task(task_id):
                    logger.error("Could not complete terminal image task %s", task_id)

    def _transition_to_retry(self, task_id: str, state: str) -> bool:
        if state == "running":
            return self.scheduler.transition_running_to_retry_wait(task_id)
        if state == "claimed":
            return self.scheduler.requeue_claimed_to_retry_wait(task_id)
        return state == "retry_wait"

    def _finish_recovered_task(
        self, task_id: str, state: str, outcome: str, metadata: Dict[str, Any]
    ) -> bool:
        if state != "running":
            return False
        if outcome == "succeeded":
            return self.scheduler.complete_task(task_id, result=metadata.get("result"))
        if outcome == "failed":
            return self.scheduler.complete_task(task_id, error="Handler execution failed")
        if outcome == "awaiting_review":
            return self.scheduler.transition_running_to_awaiting_review(task_id)
        return False

    def _setup_signals(self):
        """Set up signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down...")
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def run(self, poll_interval: int = 5, api_port: int = 11439):
        """Run the daemon loop.

        Args:
            poll_interval: Seconds between polls (default 5)
            api_port: Port for status API (default 11439)
        """
        # Start status API server
        self._start_status_api(api_port)

        logger.info(f"Starting daemon with poll interval {poll_interval}s")
        self._running = True

        try:
            while self._running:
                try:
                    if time.monotonic() - self._last_retention_cleanup >= 86400:
                        self._run_retention_cleanup()
                        self._last_retention_cleanup = time.monotonic()
                    # Try to get a queued task first
                    task = self.scheduler.get_next_task()

                    # If no queued task, check for retry_wait tasks and promote them
                    if not task:
                        retry_task = self.scheduler.get_retry_wait_task()
                        if retry_task:
                            # Promote retry_wait → queued using existing requeue_retry_wait()
                            promoted = self.scheduler.requeue_retry_wait(retry_task['id'])
                            if promoted:
                                logger.debug(f"Promoted task {retry_task['id']} from retry_wait to queued")
                                # Now get the promoted task
                                task = self.scheduler.get_next_task()
                            else:
                                logger.debug(f"Failed to promote task {retry_task['id']}, skipping")
                        else:
                            logger.debug("No tasks available (queued or retry_wait), waiting...")
                    else:
                        logger.info(f"Processing task {task['id']} (kind={task['kind']})")
                        self._dispatch_task(task)
                        continue  # Skip the no-task check below

                except Exception as e:
                    logger.exception(f"Error in daemon loop: {e}")

                time.sleep(poll_interval)
        finally:
            if self._api:
                self._api.shutdown()
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=5)
            self._api_thread = None
            logger.info("Daemon stopped")

    def _get_worker_for_task(self, task_or_kind: Any) -> Optional[str]:
        """Get an available worker for a task, including review routing.

        Returns:
            Worker name (profile) or None
        """
        task = task_or_kind if isinstance(task_or_kind, dict) else {"kind": task_or_kind}
        return self.scheduler.select_worker_for_task(task)

    def _claim_staged_input(self, task: Dict[str, Any]) -> bool:
        """Move an ingress file to processing before a worker can read it."""
        input_path = task.get("input_path")
        if not input_path:
            return False
        try:
            Path(input_path).resolve().relative_to(self.storage.incoming_path.resolve())
        except ValueError:
            return False
        metadata = self.storage.claim_for_processing(task["id"])
        if metadata is None:
            return False
        processing_path = self.storage.input_path(task["id"])
        if processing_path is None:
            return False
        task["input_path"] = str(processing_path)
        payload = dict(task.get("payload") or {})
        payload["image_path"] = str(processing_path)
        task["payload"] = payload
        return True

    def _requeue_staged_input(self, task_id: str) -> bool:
        try:
            return self.storage.requeue_for_retry(task_id)
        except (OSError, ValueError) as exc:
            logger.error("Could not requeue durable input %s: %s", task_id, exc)
            return False

    def _complete_staged_input(
        self, task_id: str, result: Optional[Dict[str, Any]] = None,
        outcome: Optional[str] = None,
    ) -> bool:
        try:
            return self.storage.complete_task(task_id, result=result, outcome=outcome)
        except (OSError, ValueError) as exc:
            logger.error("Could not complete durable input %s: %s", task_id, exc)
            return False

    def _dispatch_task(self, task: Dict[str, Any]) -> bool:
        """Execute task with proper state machine flow.

        Flow:
        1. claim_task() - atomically claim the task
        2. start_task() - atomically start with P40 locking
        3. Execute handler
        4. Transition to final state (succeeded/failed/retry_wait/awaiting_review)

        Args:
            task: Task dict from get_next_task()

        Returns:
            True if successfully processed, False on error
        """
        task_id = task['id']
        task_kind = task['kind']

        logger.info(f"Dispatching task {task_id} (kind={task_kind})")

        # Step 1: Claim the task
        claimed_task = self.scheduler.claim_task(task_id)
        if not claimed_task:
            logger.warning(f"Failed to claim task {task_id} - may already be claimed or invalid state")
            return False

        logger.info(f"Task {task_id} claimed")

        # Step 2: Claim durable image storage before a worker can read it.
        storage_claimed = self._claim_staged_input(task)
        if task.get("input_path") and not storage_claimed:
            try:
                Path(task["input_path"]).resolve().relative_to(self.storage.incoming_path.resolve())
                self.scheduler.requeue_claimed_to_retry_wait(task_id)
                return False
            except ValueError:
                pass

        # Step 3: Start the task (with P40 atomic locking)
        # Get worker for this task kind
        worker_name = self._get_worker_for_task(task)
        if not worker_name:
            logger.error(f"No suitable worker found for task kind {task_kind}")
            if storage_claimed and not self._requeue_staged_input(task_id):
                logger.error("Leaving task %s claimed for storage recovery", task_id)
                return False
            # Task cannot be processed - requeue to retry_wait
            if not self.scheduler.requeue_claimed_to_retry_wait(task_id):
                logger.error("Could not move task %s to retry_wait", task_id)
                return False
            logger.info(f"Task {task_id} requeued to retry_wait - no worker available")
            return False

        # Start task with worker profile
        if not self.scheduler.start_task(task_id, worker_profile=worker_name):
            if storage_claimed and not self._requeue_staged_input(task_id):
                logger.error("Leaving task %s claimed for storage recovery", task_id)
                return False
            logger.error(f"Failed to start task {task_id} - worker unavailable or resource conflict")
            # Task cannot be started - requeue to retry_wait
            if not self.scheduler.requeue_claimed_to_retry_wait(task_id):
                logger.error("Could not move task %s to retry_wait", task_id)
                return False
            logger.info(f"Task {task_id} requeued to retry_wait - worker unavailable or P40 busy")
            return False

        logger.info(f"Task {task_id} started with worker {worker_name}")

        # Handlers need the resolved profile to select the matching executor
        # command. Keep the worker decision durable in scheduler state while
        # exposing it explicitly to the in-process handler.
        task["worker_profile"] = worker_name

        # Mark as active
        self._active_tasks[task_id] = True

        try:
            # Step 3: Execute handler
            handler = get_handler_for_task(task)
            if not handler:
                logger.error(f"No handler found for task kind {task_kind}")
                storage_ready = not storage_claimed or self._complete_staged_input(
                    task_id, {"error": "no handler"}, "failed"
                )
                if not storage_ready:
                    return False
                return self.scheduler.complete_task(
                    task_id, error="No handler found for task kind"
                )

            # Execute the handler under a temporary P40 profile when an
            # explicit switch command is configured. GTX tasks never enter
            # this boundary.
            worker = self.scheduler._worker_registry.get_worker(worker_name)
            model_profile = (
                worker.model_profile
                if worker and worker.exclusive_resource == "p40"
                else None
            )
            profile_context = (
                self.model_profiles.profile(model_profile)
                if model_profile
                else nullcontext()
            )
            try:
                with profile_context:
                    result = handler.execute(task)
            except ModelProfileError as exc:
                logger.error("P40 model profile boundary failed for %s: %s", task_id, exc)
                # WRONG_MODEL_LOADED remediation: retry once more after ensuring profile
                logger.info("Attempting WRONG_MODEL_LOADED remediation for %s", task_id)
                if self.model_profiles.ensure_profile(model_profile):
                    # Profile switched successfully, retry task
                    logger.info("Profile remediation succeeded for %s", task_id)
                    try:
                        with self.model_profiles.profile(model_profile):
                            result = handler.execute(task)
                    except ModelProfileError as retry_exc:
                        logger.error(
                            "P40 model profile remediation boundary failed again "
                            "for %s: %s",
                            task_id,
                            retry_exc,
                        )
                        result = HandlerResult.RETRY
                    else:
                        if result == HandlerResult.FAILED:
                            result = HandlerResult.RETRY  # Retry on second failure
                else:
                    logger.error("Profile remediation failed for %s", task_id)
                    result = HandlerResult.RETRY

            # Step 4: Transition to final state based on result
            if result == HandlerResult.SUCCESS:
                # Task completed successfully
                handler_result = getattr(handler, "last_result", None)
                result_payload = handler_result if isinstance(handler_result, dict) else None
                storage_ready = not storage_claimed or self._complete_staged_input(
                    task_id, result_payload, "succeeded"
                )
                if not storage_ready:
                    logger.error("Leaving task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.complete_task(task_id, result=result_payload):
                    logger.error("Storage completed task %s but scheduler update failed", task_id)
                    return False
                logger.info(f"Task {task_id} completed successfully")

            elif result == HandlerResult.FAILED:
                # Task failed
                storage_ready = not storage_claimed or self._complete_staged_input(
                    task_id, {"error": "handler execution failed"}, "failed"
                )
                if not storage_ready:
                    logger.error("Leaving failed task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.complete_task(task_id, error="Handler execution failed"):
                    logger.error("Storage completed failed task %s but scheduler update failed", task_id)
                    return False
                logger.warning(f"Task {task_id} failed during handler execution")

            elif result == HandlerResult.RETRY:
                # Task should retry later
                storage_ready = not storage_claimed or self._requeue_staged_input(task_id)
                if not storage_ready:
                    logger.error("Leaving retrying task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.transition_running_to_retry_wait(task_id):
                    logger.error("Storage requeued task %s but scheduler update failed", task_id)
                    return False
                logger.info(f"Task {task_id} transitioned to retry_wait")

            elif result == HandlerResult.WORKER_UNAVAILABLE:
                # Worker not available (e.g., model not loaded)
                # Mark as retry_wait to retry later
                storage_ready = not storage_claimed or self._requeue_staged_input(task_id)
                if not storage_ready:
                    logger.error("Leaving unavailable task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.transition_running_to_retry_wait(task_id):
                    logger.error("Storage requeued task %s but scheduler update failed", task_id)
                    return False
                logger.info(f"Task {task_id} transitioned to retry_wait - worker unavailable")

            elif result == HandlerResult.AWAITING_REVIEW:
                # Task needs review
                handler_result = getattr(handler, "last_result", None)
                review_result = {
                    "status": "awaiting_review",
                    "vision_result": handler_result if isinstance(handler_result, dict) else None,
                }
                storage_ready = not storage_claimed or self._complete_staged_input(
                    task_id, review_result, "awaiting_review"
                )
                if not storage_ready:
                    logger.error("Leaving review task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.transition_running_to_awaiting_review(task_id):
                    logger.error("Storage completed review task %s but scheduler update failed", task_id)
                    return False
                logger.info(f"Task {task_id} marked for review")

            else:
                # Unknown result - mark as failed
                error = f"Unknown handler result: {result}"
                storage_ready = not storage_claimed or self._complete_staged_input(
                    task_id, {"error": error}, "failed"
                )
                if not storage_ready:
                    logger.error("Leaving unknown-result task %s running for storage recovery", task_id)
                    return False
                if not self.scheduler.complete_task(task_id, error=error):
                    logger.error("Storage completed task %s but scheduler update failed", task_id)
                    return False
                logger.error(f"Task {task_id} resulted in unknown state: {result}")

            return True

        except Exception as e:
            # Unexpected error - mark as failed
            logger.exception(f"Unexpected error during task {task_id} execution: {e}")
            storage_ready = not storage_claimed or self._complete_staged_input(
                task_id, {"error": str(e)}, "failed"
            )
            if storage_ready:
                self.scheduler.complete_task(task_id, error=str(e))
            return False

        finally:
            # Clean up active tasks tracking
            if task_id in self._active_tasks:
                del self._active_tasks[task_id]


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=os.getenv("GTX_BROKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    SchedulerDaemon(SchedulerConfig()).run(
        poll_interval=float(os.getenv("GTX_BROKER_POLL_INTERVAL", "5")),
        api_port=SchedulerDaemon._configured_status_api_port(),
    )
