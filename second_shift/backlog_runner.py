"""Deterministic one-item backlog queue for the GTX broker workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .backlog_manifest import BacklogItem, BacklogStatus, load_backlog_items
from .state import StateFile


class ItemLifecycle(Enum):
    """Lifecycle states owned by the queue after an item is claimed."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class RunnerConfig:
    """Configuration for one deterministic backlog queue."""

    manifest_path: Path
    state_dir: Optional[str] = None


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of handing one claimed item to the broker dispatcher."""

    item_id: str
    routing: Dict[str, Any]
    status: BacklogStatus = BacklogStatus.IN_PROGRESS


class BacklogRunner:
    """Claim and route at most one backlog item per call.

    The manifest is the ordered input.  ``StateFile`` owns mutable lifecycle
    state, so a restart resumes from the last atomically persisted status.
    Routing an item does not mark it completed: the worker/reviewer workflow
    must call :meth:`record_outcome` with the observed result.
    """

    ITEMS_KEY = "backlog_items"
    RUNNER_KEY = "backlog_runner"

    def __init__(self, config: RunnerConfig):
        self.config = config
        self.state_file = StateFile(state_dir=config.state_dir)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _persisted_items(self, registry: Optional[dict] = None) -> dict[str, BacklogItem]:
        if registry is None:
            registry = self.state_file.get_registry()
        records = registry.get(self.ITEMS_KEY, [])
        if not isinstance(records, list):
            return {}
        result: dict[str, BacklogItem] = {}
        for record in records:
            if isinstance(record, dict):
                item = BacklogItem.from_dict(record)
                result[item.issue_id] = item
        return result

    def _overlay_registry_items(self, registry: dict) -> List[BacklogItem]:
        """Load the manifest with lifecycle state from a supplied snapshot."""
        persisted = self._persisted_items(registry)
        items = load_backlog_items(self.config.manifest_path)
        return [
            replace(
                item,
                status=persisted[item.issue_id].status,
                created_at=persisted[item.issue_id].created_at,
                updated_at=persisted[item.issue_id].updated_at,
            )
            if item.issue_id in persisted
            else item
            for item in items
        ]

    def load_manifest(self) -> List[BacklogItem]:
        """Load the manifest and overlay durable lifecycle state by issue ID."""
        return self._overlay_registry_items(self.state_file.get_registry())

    @staticmethod
    def get_queued_items(items: Iterable[BacklogItem]) -> List[BacklogItem]:
        """Return queued items in their original manifest order."""
        return [item for item in items if item.status == BacklogStatus.QUEUED]

    def select_next_item(self, items: Iterable[BacklogItem]) -> Optional[BacklogItem]:
        """Select the first queued item unless one is already in progress."""
        materialized = list(items)
        if any(item.status == BacklogStatus.IN_PROGRESS for item in materialized):
            return None
        return next(iter(self.get_queued_items(materialized)), None)

    def transition_item_state(
        self,
        items: List[BacklogItem],
        item: BacklogItem,
        new_status: BacklogStatus,
        *,
        reason: Optional[str] = None,
    ) -> List[BacklogItem]:
        """Atomically update only ``item`` and append an audit transition."""
        if not isinstance(new_status, BacklogStatus):
            raise ValueError("new_status must be a BacklogStatus")
        timestamp = self._now()
        updated_items: List[BacklogItem] = []
        found = False
        for current in items:
            if current.issue_id == item.issue_id:
                found = True
                updated_items.append(
                    replace(current, status=new_status, updated_at=timestamp)
                )
            else:
                updated_items.append(current)
        if not found:
            raise ValueError(f"item is not present in manifest: {item.issue_id}")

        serialized = [current.to_dict() for current in updated_items]

        def mutate(registry: dict, entries: list[dict]):
            registry[self.ITEMS_KEY] = serialized
            runner_state = registry.setdefault(self.RUNNER_KEY, {})
            runner_state["last_updated"] = timestamp
            if new_status == BacklogStatus.IN_PROGRESS:
                runner_state["claimed_count"] = runner_state.get("claimed_count", 0) + 1
            elif new_status == BacklogStatus.COMPLETED:
                runner_state["completed_count"] = runner_state.get("completed_count", 0) + 1
            elif new_status in {BacklogStatus.BLOCKED, BacklogStatus.ESCALATED}:
                runner_state["error_count"] = runner_state.get("error_count", 0) + 1
            entries.append(
                {
                    "ledger_type": "backlog_state_transition",
                    "issue_id": item.issue_id,
                    "old_status": item.status.value,
                    "new_status": new_status.value,
                    "reason": reason,
                    "timestamp": timestamp,
                }
            )
            return registry, entries

        self.state_file.update_state(mutate)
        return updated_items

    def set_in_progress(self, items, item):
        return self.transition_item_state(items, item, BacklogStatus.IN_PROGRESS)

    def set_completed(self, items, item):
        return self.transition_item_state(items, item, BacklogStatus.COMPLETED)

    def set_blocked(self, items, item, *, reason=None):
        return self.transition_item_state(
            items, item, BacklogStatus.BLOCKED, reason=reason
        )

    def set_escalated(self, items, item, *, reason=None):
        return self.transition_item_state(
            items, item, BacklogStatus.ESCALATED, reason=reason
        )

    def _claim_next_item(self) -> tuple[List[BacklogItem], Optional[BacklogItem]]:
        """Atomically select and claim the next item from durable state."""
        result: dict[str, Any] = {"items": [], "item": None}

        def mutate(registry: dict, entries: list[dict]):
            current = self._overlay_registry_items(registry)
            result["items"] = current
            item = self.select_next_item(current)
            if item is None:
                return registry, entries

            timestamp = self._now()
            updated_items = [
                replace(current_item, status=BacklogStatus.IN_PROGRESS, updated_at=timestamp)
                if current_item.issue_id == item.issue_id
                else current_item
                for current_item in current
            ]
            registry[self.ITEMS_KEY] = [entry.to_dict() for entry in updated_items]
            runner_state = registry.setdefault(self.RUNNER_KEY, {})
            runner_state["last_updated"] = timestamp
            runner_state["claimed_count"] = runner_state.get("claimed_count", 0) + 1
            entries.append(
                {
                    "ledger_type": "backlog_state_transition",
                    "issue_id": item.issue_id,
                    "old_status": item.status.value,
                    "new_status": BacklogStatus.IN_PROGRESS.value,
                    "reason": None,
                    "timestamp": timestamp,
                }
            )
            result["items"] = updated_items
            result["item"] = next(
                entry for entry in updated_items if entry.issue_id == item.issue_id
            )
            return registry, entries

        self.state_file.update_state(mutate)
        return result["items"], result["item"]

    @staticmethod
    def _task_description(item: BacklogItem) -> str:
        criteria = "\n".join(f"- {criterion}" for criterion in item.acceptance_criteria)
        return (
            f"{item.title}\n\n{item.description}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )

    def delegate_to_dispatcher(
        self,
        item: BacklogItem,
        dispatcher_func: Callable[..., Dict[str, Any]],
        *,
        workflow: str = "gtx_direct_escalation",
    ) -> Dict[str, Any]:
        """Route one item through the real durable GTX dispatcher API."""
        return dispatcher_func(
            goal_id=item.issue_id,
            goal_class="coding",
            registry=self.state_file.get_registry(),
            workflow=workflow,
            task_description=self._task_description(item),
            gtx_context_size=item.context_size,
            gtx_quality_importance=item.quality_importance,
            requires_benchmark_evidence=item.requires_benchmark_evidence,
        )

    def dispatch_next(
        self,
        dispatcher_func: Callable[..., Dict[str, Any]],
        items: Optional[List[BacklogItem]] = None,
        *,
        workflow: str = "gtx_direct_escalation",
    ) -> tuple[List[BacklogItem], Optional[DispatchOutcome]]:
        """Claim and route exactly one queued item.

        The item remains ``in_progress`` until the caller records the worker
        result.  A dispatcher exception blocks the item and is auditable.
        """
        # ``items`` is retained for API compatibility, but durable state is
        # authoritative so stale caller snapshots cannot double-claim work.
        current, item = self._claim_next_item()
        if item is None:
            return current, None
        try:
            routing = self.delegate_to_dispatcher(
                item, dispatcher_func, workflow=workflow
            )
        except Exception as exc:
            claimed_item = next(
                entry for entry in current if entry.issue_id == item.issue_id
            )
            current = self.set_blocked(current, claimed_item, reason=str(exc))
            return current, DispatchOutcome(
                item_id=item.issue_id,
                routing={"error": str(exc)},
                status=BacklogStatus.BLOCKED,
            )
        return current, DispatchOutcome(
            item_id=item.issue_id,
            routing=routing,
            status=BacklogStatus.IN_PROGRESS,
        )

    def run_one_iteration(self, items, dispatcher_func, *, workflow="gtx_direct_escalation"):
        """Compatibility alias for :meth:`dispatch_next`."""
        return self.dispatch_next(dispatcher_func, items, workflow=workflow)

    def record_outcome(
        self,
        items: List[BacklogItem],
        item_id: str,
        status: BacklogStatus,
        *,
        reason: Optional[str] = None,
    ) -> List[BacklogItem]:
        """Persist the worker/reviewer outcome for a claimed item."""
        item = next((entry for entry in items if entry.issue_id == item_id), None)
        if item is None:
            raise ValueError(f"unknown backlog item: {item_id}")
        return self.transition_item_state(items, item, status, reason=reason)

    def get_state(self) -> dict:
        """Return durable runner counters for status reporting."""
        return self.state_file.get_registry().get(self.RUNNER_KEY, {})
