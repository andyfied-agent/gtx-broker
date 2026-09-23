"""Backlog manifest contract for bounded task classification.

Defines a repo-native machine-readable backlog manifest using Python
stdlib and dataclasses. Provides BacklogItem model with validation
for task classification metadata.

Required fields per item:
- issue_id: unique string identifier
- title: task description
- description: full task specification
- acceptance_criteria: list of acceptance criteria strings
- context_size: context budget in tokens (positive, <= P40 max)
- quality_importance: priority score (0.0 <= x <= 1.0)
- requires_benchmark_evidence: boolean flag
- status: current state of the backlog item
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import json
from pathlib import Path


# P40 GPU maximum context size: 262144 tokens (256k)
P40_MAX_CONTEXT_SIZE = 262144


class BacklogStatus(Enum):
    """Valid statuses for a backlog item."""
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ESCALATED = "escalated"


@dataclass
class BacklogItem:
    """A single backlog item representing a bounded implementation task.

    Attributes:
        issue_id: Unique identifier for the issue/task.
        title: Brief title describing the task.
        description: Full description of what needs to be done.
        acceptance_criteria: List of criteria that define completion.
        context_size: Maximum context size in tokens (1 <= x <= P40_MAX_CONTEXT_SIZE).
        quality_importance: Quality/importance weight (0.0 <= x <= 1.0).
        requires_benchmark_evidence: Whether benchmark evidence is required.
        status: Current lifecycle status of this backlog item.
    """
    issue_id: str
    title: str
    description: str
    acceptance_criteria: List[str]
    context_size: int
    quality_importance: float
    requires_benchmark_evidence: bool
    status: BacklogStatus
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    _raw_data: dict = field(default=None, repr=False, compare=False, init=False)

    @staticmethod
    def from_dict(data: dict) -> BacklogItem:
        """Create a BacklogItem from a dictionary.

        Args:
            data: Dictionary containing backlog item fields.

        Returns:
            A BacklogItem instance.

        Raises:
            ManifestValidationError: If required fields are missing or invalid.
        """
        if not isinstance(data, dict):
            raise ManifestValidationError("backlog item must be an object")
        data = data.copy()

        # Validate required fields before construction
        required_fields = [
            'issue_id', 'title', 'description', 'acceptance_criteria',
            'context_size', 'quality_importance', 'requires_benchmark_evidence', 'status'
        ]
        allowed_fields = set(required_fields) | {'created_at', 'updated_at'}
        unknown_fields = sorted(set(data) - allowed_fields)
        if unknown_fields:
            raise ManifestValidationError(
                f"unknown field(s): {', '.join(unknown_fields)}"
            )
        for field_name in required_fields:
            if field_name not in data:
                raise ManifestValidationError(
                    f"required field '{field_name}' is missing"
                )

        # Convert status to enum - catch invalid status values
        status_value = data.pop('status')
        try:
            data['status'] = BacklogStatus(status_value)
        except ValueError as e:
            raise ManifestValidationError(
                f"status must be one of: {[e.value for e in BacklogStatus]}; got '{status_value}'"
            ) from e

        return BacklogItem(**data)

    def to_dict(self) -> dict:
        """Convert a BacklogItem to a dictionary for serialization."""
        return {
            'issue_id': self.issue_id,
            'title': self.title,
            'description': self.description,
            'acceptance_criteria': self.acceptance_criteria,
            'context_size': self.context_size,
            'quality_importance': self.quality_importance,
            'requires_benchmark_evidence': self.requires_benchmark_evidence,
            'status': self.status.value,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }

    def __post_init__(self):
        """Validate item after initialization."""
        validate_backlog_item(self)


class ManifestValidationError(ValueError):
    """Raised when manifest validation fails."""
    pass


def validate_backlog_item(item: BacklogItem) -> None:
    """Validate a single BacklogItem according to contract rules.

    Raises:
        ManifestValidationError: If the item violates any constraint.
    """
    # Validate non-empty required string fields
    for field_name in ('issue_id', 'title', 'description'):
        value = getattr(item, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ManifestValidationError(
                f"{field_name} is required and must be a non-empty string"
            )

    # Validate acceptance criteria is non-empty list
    if not item.acceptance_criteria:
        raise ManifestValidationError("acceptance_criteria is required and must be non-empty")
    if not isinstance(item.acceptance_criteria, list):
        raise ManifestValidationError("acceptance_criteria must be a list")
    for i, criterion in enumerate(item.acceptance_criteria):
        if not isinstance(criterion, str) or not criterion.strip():
            raise ManifestValidationError(
                f"acceptance_criteria[{i}] must be a non-empty string"
            )

    # Validate context_size is positive and within P40 maximum
    if isinstance(item.context_size, bool) or not isinstance(item.context_size, int):
        raise ManifestValidationError("context_size must be an integer")
    if item.context_size <= 0:
        raise ManifestValidationError("context_size must be positive")
    if item.context_size > P40_MAX_CONTEXT_SIZE:
        raise ManifestValidationError(
            f"context_size must be <= {P40_MAX_CONTEXT_SIZE} (P40 maximum)"
        )

    # Validate quality_importance is in [0.0, 1.0]
    if isinstance(item.quality_importance, bool) or not isinstance(item.quality_importance, (int, float)):
        raise ManifestValidationError("quality_importance must be a number")
    if not (0.0 <= item.quality_importance <= 1.0):
        raise ManifestValidationError("quality_importance must be between 0.0 and 1.0")

    # Validate requires_benchmark_evidence is boolean
    if not isinstance(item.requires_benchmark_evidence, bool):
        raise ManifestValidationError("requires_benchmark_evidence must be a boolean")

    # Validate status is a valid BacklogStatus
    if not isinstance(item.status, BacklogStatus):
        raise ManifestValidationError(
            f"status must be one of: {[e.value for e in BacklogStatus]}"
        )


def load_manifest(path: Path) -> dict:
    """Load and validate a backlog manifest JSON file.

    Args:
        path: Path to the manifest JSON file.

    Returns:
        Validated manifest dictionary containing 'items' list.

    Raises:
        ManifestValidationError: If the manifest is invalid.
        FileNotFoundError: If the path does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return validate_manifest(data)


def validate_manifest(data: dict) -> dict:
    """Validate a backlog manifest dictionary.

    Args:
        data: The manifest dictionary to validate.

    Returns:
        The validated manifest dictionary.

    Raises:
        ManifestValidationError: If the manifest is invalid.
    """
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest must be a JSON object")

    if 'items' not in data:
        raise ManifestValidationError("manifest must contain 'items' array")

    items = data['items']
    if not isinstance(items, list):
        raise ManifestValidationError("'items' must be an array")

    seen_ids: set[str] = set()

    for index, item_data in enumerate(items):
        if not isinstance(item_data, dict):
            raise ManifestValidationError(f"items[{index}] must be an object")

        # Validate each item
        try:
            item = BacklogItem.from_dict(item_data)
            # Validation happens in __post_init__ via validate_backlog_item
        except (ManifestValidationError, ValueError) as e:
            raise ManifestValidationError(
                f"items[{index}] validation failed: {e}"
            ) from e

        # Check duplicates only after the item has passed schema validation, so
        # malformed unhashable IDs cannot escape as a raw TypeError.
        if item.issue_id in seen_ids:
            raise ManifestValidationError(f"duplicate issue_id found: {item.issue_id}")
        seen_ids.add(item.issue_id)

    return data


def load_backlog_items(path: Path) -> List[BacklogItem]:
    """Load and parse a manifest file into a list of BacklogItem objects.

    Args:
        path: Path to the manifest JSON file.

    Returns:
        List of validated BacklogItem objects.
    """
    manifest = load_manifest(path)
    return [BacklogItem.from_dict(item_data) for item_data in manifest['items']]


def save_manifest(path: Path, items: List[BacklogItem], overwrite: bool = True) -> None:
    """Save a list of BacklogItem objects to a manifest JSON file.

    Args:
        path: Path to save the manifest to.
        items: List of BacklogItem objects to serialize.
        overwrite: If False, raise FileNotFoundError if path exists.

    Raises:
        FileExistsError: If overwrite=False and path exists.
    """
    if not overwrite and path.exists():
        raise FileExistsError(f"Manifest file already exists: {path}")

    data = {
        'items': [item.to_dict() for item in items],
    }

    # Validate the complete serialized manifest before creating directories or
    # writing anything. This catches duplicate IDs introduced by the caller.
    validate_manifest(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
