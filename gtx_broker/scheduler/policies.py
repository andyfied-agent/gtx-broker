"""Daily dispatch policy implementation.

Implements the initial daily dispatch policy from Vision Scheduler Architecture:
- Immediate: coding → P40, queries → GTX
- Images: staged on receipt, processed at 00:00
- Batch work: starts after image queue empty, runs until 06:00
- Immediate work always outranks scheduled work
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from enum import Enum


class TaskMode(Enum):
    """Task execution mode."""
    IMMEDIATE = "immediate"  # Quick response, no waiting
    BATCH = "batch"  # Can wait for scheduled window
    VISION = "vision"  # Image processing (scheduled at 00:00)
    MAINTENANCE = "maintenance"  # Low priority, preemptible


class ScheduleWindow(Enum):
    """Current schedule window."""
    IMMEDIATE = "immediate"  # Now until 06:00 for immediate work
    IMAGE_WINDOW = "image_window"  # 00:00-06:00 for image processing
    BATCH_WINDOW = "batch_window"  # After images empty until 06:00
    RESTRICTED = "restricted"  # After 06:00, no new batch tasks


@dataclass
class DispatchPolicy:
    """Current dispatch policy configuration."""
    image_window_start_hour: int = 0  # 00:00
    image_window_end_hour: int = 6  # 06:00
    batch_allowed_after_images: bool = True
    immediate_priority: int = 100  # Highest priority
    batch_priority: int = 10
    vision_priority: int = 50
    maintenance_priority: int = 1


class DailyDispatchPolicy:
    """Implements daily dispatch policy from scheduler architecture.

    The scheduler must expose:
    - Current active window
    - Queue depths by mode
    - Deferred work and reasons
    - Whether a task is waiting and why
    """

    def __init__(self, policy: Optional[DispatchPolicy] = None):
        """Initialize dispatch policy.

        Args:
            policy: Policy configuration (uses defaults if None)
        """
        self.policy = policy or DispatchPolicy()

    def _now(self) -> datetime:
        """Return current UTC datetime."""
        return datetime.now(timezone.utc)

    def _get_local_time(self) -> datetime:
        """Return current time in compute01's timezone (Europe/London)."""
        # TODO: Detect actual timezone from system or config
        # For now, assume Europe/London
        from datetime import timezone as tz
        london_tz = tz(timedelta(hours=0))  # UTC (BST is UTC+1, handled separately)
        return datetime.now(london_tz)

    def get_current_window(self, local_time: Optional[datetime] = None) -> ScheduleWindow:
        """Determine current schedule window.

        Args:
            local_time: Current time (uses system time if None)

        Returns:
            Current schedule window
        """
        if local_time is None:
            local_time = self._get_local_time()

        hour = local_time.hour

        # After 06:00 until midnight: restricted
        if hour >= self.policy.image_window_end_hour and hour < 24:
            return ScheduleWindow.RESTRICTED

        # Between midnight and 06:00: could be image or batch window
        if hour >= self.policy.image_window_start_hour:
            return ScheduleWindow.IMAGE_WINDOW

        return ScheduleWindow.RESTRICTED

    def should_queue_immediately(self, task_mode: TaskMode,
                                  pending_vision: int = 0) -> bool:
        """Determine if a task should be queued immediately.

        Args:
            task_mode: Task execution mode
            pending_vision: Number of pending vision tasks

        Returns:
            True if task should start immediately
        """
        if task_mode == TaskMode.IMMEDIATE:
            return True

        if task_mode == TaskMode.VISION:
            # Vision tasks wait for 00:00 window
            return False

        if task_mode == TaskMode.BATCH:
            # Batch can run after image window
            # Check if we're in image window with pending images
            window = self.get_current_window()
            if window == ScheduleWindow.IMAGE_WINDOW and pending_vision > 0:
                return False
            return True

        return False

    def calculate_priority(self, task_mode: TaskMode, urgency: str = "normal") -> int:
        """Calculate task priority based on mode and urgency.

        Args:
            task_mode: Task execution mode
            urgency: "low", "normal", "high", "urgent"

        Returns:
            Priority value (higher = more urgent)
        """
        base_priorities = {
            TaskMode.IMMEDIATE: self.policy.immediate_priority,
            TaskMode.VISION: self.policy.vision_priority,
            TaskMode.BATCH: self.policy.batch_priority,
            TaskMode.MAINTENANCE: 0,
        }

        priority = base_priorities.get(task_mode, 0)

        # Adjust for urgency
        urgency_adjustments = {
            "urgent": 50,
            "high": 25,
            "normal": 0,
            "low": -10,
        }
        priority += urgency_adjustments.get(urgency, 0)

        return priority

    def can_admit_batch_task(self, pending_vision: int = 0,
                              pending_batch: int = 0) -> bool:
        """Check if new batch tasks can be admitted.

        Args:
            pending_vision: Number of pending vision tasks
            pending_batch: Number of pending batch tasks

        Returns:
            True if batch tasks can be admitted
        """
        window = self.get_current_window()

        # After 06:00: no new batch tasks
        if window == ScheduleWindow.RESTRICTED:
            return False

        # In image window with pending images: wait
        if window == ScheduleWindow.IMAGE_WINDOW and pending_vision > 0:
            return False

        return True

    def get_waiting_reason(self, task_mode: TaskMode,
                           pending_vision: int = 0) -> Optional[str]:
        """Get reason why a task is waiting.

        Args:
            task_mode: Task execution mode
            pending_vision: Number of pending vision tasks

        Returns:
            Reason string if waiting, None if can proceed
        """
        if task_mode == TaskMode.VISION:
            return f"Waiting for image window (starts at {self.policy.image_window_start_hour:02d}:00)"

        if task_mode == TaskMode.BATCH:
            window = self.get_current_window()

            if window == ScheduleWindow.RESTRICTED:
                return f"No new batch tasks admitted after {self.policy.image_window_end_hour:02d}:00"

            if window == ScheduleWindow.IMAGE_WINDOW and pending_vision > 0:
                return f"Waiting for {pending_vision} pending vision tasks to complete"

        return None

    def estimate_wait_time(self, task_mode: TaskMode,
                           position_in_queue: int = 0) -> Optional[timedelta]:
        """Estimate wait time for a task.

        Args:
            task_mode: Task execution mode
            position_in_queue: Position in queue (0 = first)

        Returns:
            Estimated wait time or None if immediate
        """
        if task_mode == TaskMode.IMMEDIATE:
            return None

        if task_mode == TaskMode.VISION:
            # Wait for next 00:00
            now = self._get_local_time()
            next_window = now.replace(hour=self.policy.image_window_start_hour, minute=0, second=0, microsecond=0)
            if next_window <= now:
                next_window += timedelta(days=1)
            return next_window - now

        if task_mode == TaskMode.BATCH:
            window = self.get_current_window()

            if window == ScheduleWindow.RESTRICTED:
                # Wait until next day's image window
                now = self._get_local_time()
                next_window = now.replace(hour=self.policy.image_window_start_hour, minute=0, second=0, microsecond=0)
                if next_window <= now:
                    next_window += timedelta(days=1)
                return next_window - now

            if window == ScheduleWindow.IMAGE_WINDOW:
                # Wait for pending vision tasks
                # Assume ~10 seconds per vision task
                return timedelta(seconds=position_in_queue * 10)

        return None


# Singleton instance
_default_policy = None


def get_dispatch_policy() -> DailyDispatchPolicy:
    """Get the default dispatch policy instance."""
    global _default_policy
    if _default_policy is None:
        _default_policy = DailyDispatchPolicy()
    return _default_policy
