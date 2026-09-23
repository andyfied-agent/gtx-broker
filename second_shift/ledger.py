"""Failure ledger for Second Shift model attempts."""

from __future__ import annotations

import copy
from enum import Enum
from typing import Dict, List, Optional

from .registry import ModelRegistry
from .state import StateFile


class FailureClassification(Enum):
    NO_CODE = "no_code"
    PARTIAL_CODE = "partial_code"
    INCORRECT_CODE = "incorrect_code"
    TESTS_FAILED = "tests_failed"
    TIMEOUT = "timeout"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    AUTHENTICATION = "authentication"
    CREDIT_EXHAUSTED = "credit_exhausted"
    RATE_LIMITED = "rate_limited"
    POLICY_REFUSAL = "policy_refusal"
    CONTEXT_LIMIT = "context_limit"
    UNKNOWN = "unknown"


class FailureLedger:
    def __init__(self, state_file: StateFile):
        self.state = state_file

    def classify_as_substantive(self, classification: str) -> bool:
        return classification in self.state.SUBSTANTIVE_CLASSIFICATIONS

    def classify_as_non_substantive(self, classification: str) -> bool:
        return classification in self.state.NON_SUBSTANTIVE_CLASSIFICATIONS

    def record_failure(
        self, *, model_id: str, provider: str, goal_id: str, goal_class: str,
        classification: str, evidence: List[str], registry: Dict,
        artifact: Optional[str] = None, credit_status: str = "unknown",
        reviewer: Optional[str] = None, notes: Optional[str] = None,
        pool_recovery_transition: Optional[int] = None,
        pool_recovery_action: Optional[str] = None,
    ) -> Dict:
        timestamp = self.state.timestamp_now()
        candidate = {
            "model_id": model_id, "provider": provider, "goal_id": goal_id,
            "goal_class": goal_class, "timestamp": timestamp,
            "classification": classification, "credit_status": credit_status,
            "counted_as_substantive_failure": self.classify_as_substantive(classification),
            "evidence": evidence,
        }
        errors = self.validate_entry(candidate)
        if errors:
            raise ValueError("; ".join(errors))

        supplied_registry = copy.deepcopy(registry)

        def mutate(current: Dict, entries: List[Dict]):
            # Persisted state is authoritative after initialization; the
            # supplied registry seeds a new state directory.
            current = current if current.get("models") else supplied_registry
            registry_obj = ModelRegistry(self.state)
            substantive = self.classify_as_substantive(classification)
            updated = (
                registry_obj.increment_substantive_failure(model_id, goal_class, current)
                if substantive else copy.deepcopy(current)
            )
            model = registry_obj.get_model(model_id, updated)
            count = registry_obj.get_substantive_failure_count(model_id, goal_class, updated)
            entry = {
                "model_id": model_id, "provider": provider, "goal_id": goal_id,
                "goal_class": goal_class, "timestamp": timestamp,
                "classification": classification, "credit_status": credit_status,
                "counted_as_substantive_failure": substantive,
                "consecutive_substantive_failures": count,
                "status_after_attempt": model.get("status", "unknown") if model else "unknown",
                "evidence": list(evidence),
                "pool_recovery_transition": pool_recovery_transition,
                "pool_recovery_action": pool_recovery_action,
            }
            for key, value in (("artifact", artifact), ("reviewer", reviewer), ("notes", notes)):
                if value is not None:
                    entry[key] = value
            return updated, entries + [entry]

        return self.state.update_state(mutate)

    def record_success(
        self, *, model_id: str, goal_class: str, artifact: str,
        registry: Dict,
    ) -> Dict:
        """Record a successful code artifact and reset that goal's streak.

        A successful result does not reactivate a quarantined model; reviewer
        evidence is still required through ``ModelRegistry.reactivate_model``.
        """
        if not artifact or not artifact.strip():
            raise ValueError("successful artifact reference is required")
        supplied_registry = copy.deepcopy(registry)

        def mutate(current: Dict, entries: List[Dict]):
            current = current if current.get("models") else supplied_registry
            updated = copy.deepcopy(current)
            model = ModelRegistry(self.state).get_model(model_id, updated)
            if model is None:
                raise ValueError(f"unknown model: {model_id}")
            for item in updated.get("models", []):
                if item.get("id") == model_id:
                    item.setdefault("failure_counts_by_goal", {})[goal_class] = 0
                    item["last_successful_artifact"] = artifact
                    item["last_success_at"] = self.state.timestamp_now()
                    break
            return updated, entries

        return self.state.update_state(mutate)

    def get_failures_for_model(self, model_id: str, goal_class: Optional[str] = None, registry: Optional[Dict] = None) -> List[Dict]:
        entries = [e for e in self.state.get_ledger_entries() if e.get("model_id") == model_id]
        if goal_class is not None:
            entries = [e for e in entries if e.get("goal_class") == goal_class]
        return entries

    def get_substantive_failures(self, model_id: str, goal_class: str) -> List[Dict]:
        return [e for e in self.get_failures_for_model(model_id, goal_class) if e.get("counted_as_substantive_failure")]

    def get_failure_count(self, model_id: str, goal_class: str, only_substantive: bool = True) -> int:
        entries = self.get_substantive_failures(model_id, goal_class) if only_substantive else self.get_failures_for_model(model_id, goal_class)
        return len(entries)

    def get_last_successful_artifact(self, model_id: str, registry: Dict) -> Optional[str]:
        return ModelRegistry(self.state).get_last_successful_artifact(model_id, registry)

    def get_latest_entry(self, model_id: str) -> Optional[Dict]:
        entries = self.get_failures_for_model(model_id)
        return entries[-1] if entries else None

    def clear_all(self) -> None:
        self.state.clear_ledger()

    def validate_entry(self, entry: Dict) -> List[str]:
        errors = []
        required = (
            "model_id", "provider", "goal_id", "goal_class", "timestamp",
            "classification", "credit_status", "counted_as_substantive_failure",
            "evidence",
        )
        for field in required:
            if field not in entry:
                errors.append(f"Missing required field: {field}")
        if "evidence" in entry and not isinstance(entry["evidence"], list):
            errors.append("evidence must be a list")
        elif "evidence" in entry and not entry["evidence"]:
            errors.append("evidence must be a non-empty list")
        if entry.get("classification") not in [item.value for item in FailureClassification]:
            errors.append(f"Invalid classification: {entry.get('classification')}")
        if entry.get("credit_status") not in {"available", "low", "exhausted", "unknown"}:
            errors.append(f"Invalid credit status: {entry.get('credit_status')}")
        return errors
