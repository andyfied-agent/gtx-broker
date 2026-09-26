"""Handler interfaces for task processing.

Defines the handler contract that vision.py and coding.py will implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable
from enum import Enum


class HandlerResult(Enum):
    """Result of handler execution."""
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"
    AWAITING_REVIEW = "awaiting_review"
    WORKER_UNAVAILABLE = "worker_unavailable"


@dataclass
class HandlerAttempt:
    """Record of a handler attempt."""
    attempt_number: int
    worker_profile: str
    model_profile: Optional[str]
    start_at: str
    end_at: Optional[str]
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    failure_class: Optional[str]
    tokens_used: Optional[int] = None


class TaskHandler(ABC):
    """Abstract base class for task handlers.

    Each handler type (vision, coding, data, maintenance) implements this interface.
    """

    @property
    @abstractmethod
    def handler_type(self) -> str:
        """Return the handler type (e.g., 'vision', 'coding')."""
        pass

    @abstractmethod
    def can_handle(self, task_payload: Dict[str, Any]) -> bool:
        """Check if this handler can process the task.

        Args:
            task_payload: Task payload from database

        Returns:
            True if handler can process this task
        """
        pass

    @abstractmethod
    def execute(self, task_payload: Dict[str, Any],
                metadata_path: str) -> tuple[HandlerResult, Optional[Dict[str, Any]], Optional[str]]:
        """Execute the handler on the task.

        Args:
            task_payload: Task payload
            metadata_path: Path to task metadata directory

        Returns:
            Tuple of (result, output_data, error_message)
        """
        pass

    @abstractmethod
    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate handler output.

        Args:
            output: Handler output data

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass


class VisionHandler(TaskHandler):
    """Vision task handler (implements TaskHandler).

    Processes images using P40 vision worker with approved projector.
    """

    @property
    def handler_type(self) -> str:
        return "vision"

    def can_handle(self, task_payload: Dict[str, Any]) -> bool:
        return task_payload.get("kind") == "vision"

    def execute(self, task_payload: Dict[str, Any],
                metadata_path: str) -> tuple[HandlerResult, Optional[Dict[str, Any]], Optional[str]]:
        """Execute vision task.

        Args:
            task_payload: Task payload with input_path, caption, etc.
            metadata_path: Path to task metadata

        Returns:
            Tuple of (result, output, error)
        """
        # TODO: Implement vision processing
        # 1. Load image from metadata_path/image.jpg
        # 2. Load approved projector and model profile
        # 3. Send to P40 vision endpoint (11436)
        # 4. Parse JSON response
        # 5. Validate output
        # 6. Return result

        return HandlerResult.WORKER_UNAVAILABLE, None, "Vision handler not yet implemented"

    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate vision output.

        Args:
            output: Vision extraction output

        Returns:
            Tuple of (is_valid, error_message)
        """
        # TODO: Validate vision output schema
        # Check required fields: merchant, date, totals, line_items
        return True, None


class CodingHandler(TaskHandler):
    """Coding task handler (implements TaskHandler).

    Processes coding tasks using P40 coding worker.
    """

    @property
    def handler_type(self) -> str:
        return "coding"

    def can_handle(self, task_payload: Dict[str, Any]) -> bool:
        return task_payload.get("kind") == "coding"

    def execute(self, task_payload: Dict[str, Any],
                metadata_path: str) -> tuple[HandlerResult, Optional[Dict[str, Any]], Optional[str]]:
        """Execute coding task.

        Args:
            task_payload: Task payload with goal, context, repo_path, etc.
            metadata_path: Path to task metadata

        Returns:
            Tuple of (result, output, error)
        """
        # TODO: Implement coding execution
        # 1. Clone/fetch repository
        # 2. Load prompt and context
        # 3. Send to P40 coding endpoint (11436)
        # 4. Apply changes to repository
        # 5. Run tests
        # 6. Return result

        return HandlerResult.WORKER_UNAVAILABLE, None, "Coding handler not yet implemented"

    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate coding output.

        Args:
            output: Coding result with changes, test results, etc.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # TODO: Validate coding output
        # Check: files changed, tests pass, no unintended modifications
        return True, None


# Handler factory
HANDLERS = [VisionHandler(), CodingHandler()]


def get_handler_for_task(task_payload: Dict[str, Any]) -> Optional[TaskHandler]:
    """Find handler for a task.

    Args:
        task_payload: Task payload

    Returns:
        Matching TaskHandler or None
    """
    for handler in HANDLERS:
        if handler.can_handle(task_payload):
            return handler
    return None
