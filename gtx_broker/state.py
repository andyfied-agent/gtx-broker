"""GTX broker state management and persistence.

Provides persistent state storage for the GTX broker controller using
the existing StateFile transaction mechanism. Tracks model routing decisions,
task classifications, provider transitions, and attempt histories.

Author: GTX Broker P7.5
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from enum import Enum


class ProviderStatus(Enum):
    """Status of a model/provider in the registry."""
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unusable"
    STANDBY = "standby"
    RATE_LIMITED = "rate_limited"


class GTXBrokerWorkflow(Enum):
    """Supported broker workflows."""
    DIRECT_ESCALATION = "direct_escalation"
    LAYERED_REVIEW = "layered_review"


class TaskClassification(Enum):
    """Task classification for routing."""
    BOUNDED_IMPLEMENTATION = "bounded_implementation"
    BENCHMARK_VALIDATION = "benchmark_validation"
    LARGE_CONTEXT = "large_context"
    COMPLEX_REASONING = "complex_reasoning"
    REPAIR_LOCAL = "repair_local"


class ProviderTransitionType(Enum):
    """Types of provider transitions."""
    COPILOT_TO_CODEX = "copilot_to_codex"
    GTX_TO_P40 = "gtx_to_p40"
    P40_TO_CODEX = "p40_to_codex"
    QUALITY_TO_SPEED = "quality_to_speed"


@dataclass
class ProviderTransition:
    """Record of a provider transition (failover)."""
    from_worker: str
    to_worker: str
    reason: str
    timestamp: str
    evidence: List[str] = field(default_factory=list)


@dataclass
class GTXBrokerState:
    """Persistent state for GTX broker controller.

    Tracks the current routing decision, task metadata, and transition history.
    This is the core state object that gets persisted atomically.
    """
    selected_model: str  # "gtx_iq3_xs", "gtx_q2_k", "p40_qwen35", "codex"
    workflow: GTXBrokerWorkflow  # Which workflow is active
    task_id: str
    task_class: str  # "coding", "reasoning", "review", "general"
    context_size: int  # Token context size (e.g., 64000, 262144)
    quality_preference: str  # "quality" | "speed"
    task_description: str = ""
    provider_transition: Optional[ProviderTransition] = None
    last_routing_at: str = ""
    gtx_substantive_failures: int = 0  # Count for GTX-specific failures
    p40_substantive_failures: int = 0  # Count for P40-specific failures

    # Metadata
    attempt_number: int = 1
    is_final_verification: bool = False
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "selected_model": self.selected_model,
            "workflow": self.workflow.value if self.workflow else None,
            "task_id": self.task_id,
            "task_description": self.task_description,
            "task_class": self.task_class,
            "context_size": self.context_size,
            "quality_preference": self.quality_preference,
            "provider_transition": asdict(self.provider_transition) if self.provider_transition else None,
            "last_routing_at": self.last_routing_at,
            "gtx_substantive_failures": self.gtx_substantive_failures,
            "p40_substantive_failures": self.p40_substantive_failures,
            "attempt_number": self.attempt_number,
            "is_final_verification": self.is_final_verification,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GTXBrokerState":
        """Create instance from dictionary."""
        provider_transition = None
        if data.get("provider_transition"):
            pt_data = data["provider_transition"]
            provider_transition = ProviderTransition(
                from_worker=pt_data["from_worker"],
                to_worker=pt_data["to_worker"],
                reason=pt_data["reason"],
                timestamp=pt_data["timestamp"],
                evidence=pt_data.get("evidence", []),
            )

        return cls(
            selected_model=data["selected_model"],
            workflow=GTXBrokerWorkflow(data["workflow"]) if data.get("workflow") else GTXBrokerWorkflow.DIRECT_ESCALATION,
            task_id=data["task_id"],
            task_description=data.get("task_description", ""),
            task_class=data["task_class"],
            context_size=data["context_size"],
            quality_preference=data["quality_preference"],
            provider_transition=provider_transition,
            last_routing_at=data.get("last_routing_at", ""),
            gtx_substantive_failures=data.get("gtx_substantive_failures", 0),
            p40_substantive_failures=data.get("p40_substantive_failures", 0),
            attempt_number=data.get("attempt_number", 1),
            is_final_verification=data.get("is_final_verification", False),
            evidence=data.get("evidence", []),
        )


class GTXBrokerRegistry:
    """Registry for GTX broker state management.

    Provides thread-safe access to broker state via the existing StateFile
    transaction mechanism. Manages the `gtx_broker` registry namespace.
    """

    def __init__(self, state_file):
        """Initialize with StateFile instance.

        Args:
            state_file: StateFile instance from second_shift.state
        """
        self.state = state_file

    def _now(self) -> str:
        """Return current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def get_registry(self) -> Dict:
        """Load or seed the broker registry."""
        registry = self.state.get_registry()
        if "gtx_broker" not in registry:
            registry["gtx_broker"] = {
                "current_state": None,
                "task_history": [],
                "provider_transitions": [],
                "settings": {
                    "gtx_failure_threshold": 3,
                    "p40_failure_threshold": 3,
                    "default_quality_preference": "quality",
                    "allow_direct_escalation": True,
                }
            }
        return registry

    def _save_registry(self, registry: Dict) -> None:
        """Save the broker registry."""
        self.state.save_registry(registry)

    def get_current_state(self) -> Optional[GTXBrokerState]:
        """Get the current active state."""
        registry = self.get_registry()
        state_data = registry.get("gtx_broker", {}).get("current_state")
        if state_data:
            return GTXBrokerState.from_dict(state_data)
        return None

    def get_task_state(self, task_id: str) -> Optional[GTXBrokerState]:
        """Return the newest durable state for a task from current/history."""
        registry = self.get_registry().get("gtx_broker", {})
        current = registry.get("current_state")
        if current and current.get("task_id") == task_id:
            return GTXBrokerState.from_dict(current)
        for state_data in reversed(registry.get("task_history", [])):
            if state_data.get("task_id") == task_id:
                return GTXBrokerState.from_dict(state_data)
        return None

    def set_current_state(self, state: GTXBrokerState) -> None:
        """Set the current active state."""
        registry = self.get_registry()
        registry["gtx_broker"]["current_state"] = state.to_dict()
        self._save_registry(registry)

    def get_task_history(self) -> List[Dict]:
        """Get the task history (last N decisions)."""
        registry = self.get_registry()
        return registry.get("gtx_broker", {}).get("task_history", [])

    def append_task_history(self, state: GTXBrokerState) -> None:
        """Append current state to task history."""
        registry = self.get_registry()
        history = registry.get("gtx_broker", {}).get("task_history", [])
        # Keep only last 100 entries
        history.append(state.to_dict())
        if len(history) > 100:
            history = history[-100:]
        registry["gtx_broker"]["task_history"] = history
        self._save_registry(registry)

    def get_provider_transitions(self) -> List[Dict]:
        """Get all provider transitions."""
        registry = self.get_registry()
        return registry.get("gtx_broker", {}).get("provider_transitions", [])

    def append_provider_transition(self, transition: ProviderTransition) -> None:
        """Append a provider transition record."""
        registry = self.get_registry()
        transitions = registry.get("gtx_broker", {}).get("provider_transitions", [])
        transitions.append(asdict(transition))
        registry["gtx_broker"]["provider_transitions"] = transitions
        self._save_registry(registry)

    def get_settings(self) -> Dict:
        """Get current broker settings."""
        registry = self.get_registry()
        return registry.get("gtx_broker", {}).get("settings", {
            "gtx_failure_threshold": 3,
            "p40_failure_threshold": 3,
            "default_quality_preference": "quality",
            "allow_direct_escalation": True,
        })

    def update_settings(self, updates: Dict[str, Any]) -> Dict:
        """Update broker settings atomically."""
        registry = self.get_registry()
        settings = registry.get("gtx_broker", {}).get("settings", {})
        settings.update(updates)
        registry["gtx_broker"]["settings"] = settings
        self._save_registry(registry)
        return settings

    def increment_gtx_failures(self, task_id: str) -> int:
        """Increment GTX failure count for a task."""
        registry = self.get_registry()
        gtx_broker = registry.get("gtx_broker", {})
        current_state = gtx_broker.get("current_state", {})

        if current_state and current_state.get("task_id") == task_id:
            current_state["gtx_substantive_failures"] = current_state.get("gtx_substantive_failures", 0) + 1
            registry["gtx_broker"]["current_state"] = current_state
            self._save_registry(registry)

        return current_state.get("gtx_substantive_failures", 0)

    def increment_p40_failures(self, task_id: str) -> int:
        """Increment P40 failure count for a task."""
        registry = self.get_registry()
        gtx_broker = registry.get("gtx_broker", {})
        current_state = gtx_broker.get("current_state", {})

        if current_state and current_state.get("task_id") == task_id:
            current_state["p40_substantive_failures"] = current_state.get("p40_substantive_failures", 0) + 1
            registry["gtx_broker"]["current_state"] = current_state
            self._save_registry(registry)

        return current_state.get("p40_substantive_failures", 0)


__all__ = [
    "ProviderStatus",
    "GTXBrokerWorkflow",
    "TaskClassification",
    "ProviderTransitionType",
    "ProviderTransition",
    "GTXBrokerState",
    "GTXBrokerRegistry",
]
