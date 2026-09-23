"""Credit-state tracking for Second Shift models.

P7.2: Independent credit-state tracking with exactly available/low/exhausted/unknown.
Exhausted may be set only from explicit provider/account evidence.
Timeout, no response, empty output, no_code, and generic endpoint failure
must leave credits unknown or unchanged.
Preserve implementation-failure counts and model status as separate state.
Persist credit transitions atomically with registry/ledger state.
Record evidence and timestamp for explicit provider credit evidence.
Validate invalid states.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Set

from .state import StateFile


class CreditStatus:
    """Valid credit states."""
    AVAILABLE = "available"
    LOW = "low"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"
    VALID_STATES: Set[str] = frozenset({AVAILABLE, LOW, EXHAUSTED, UNKNOWN})


class ModelCreditState:
    """Pure credit-state transformations plus durable load/save helpers."""

    SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "partial_code", "incorrect_code", "tests_failed",
    })
    NON_SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "no_code", "timeout", "endpoint_unavailable", "authentication",
        "credit_exhausted", "rate_limited", "policy_refusal", "context_limit",
        "review_unavailable", "unknown",
    })

    # Classifications that constitute explicit provider credit evidence
    EXPLICIT_CREDIT_EVIDENCE_CLASSIFICATIONS = frozenset({
        "credit_exhausted", "rate_limited",
    })

    def __init__(self, state_file: StateFile):
        self.state = state_file

    def load_registry(self) -> Dict:
        return self.state.get_registry()

    def save_registry(self, registry: Dict) -> None:
        self.state.save_registry(registry)

    def get_model(self, model_id: str, registry: Dict) -> Optional[Dict]:
        for model in registry.get("models", []):
            if model.get("id") == model_id:
                return copy.deepcopy(model)
        return None

    def get_credit_status(self, model_id: str, registry: Dict) -> Optional[str]:
        model = self.get_model(model_id, registry)
        return model.get("credit_status", "unknown") if model else "unknown"

    def validate_credit_status(self, status: str) -> List[str]:
        """Validate a credit status is one of: available, low, exhausted, unknown."""
        errors = []
        if status not in CreditStatus.VALID_STATES:
            errors.append(f"Invalid credit status: {status}. Must be one of: {', '.join(sorted(CreditStatus.VALID_STATES))}")
        return errors

    def update_model_credit_status(
        self,
        registry: Dict,
        model_id: str,
        credit_status: str,
        *,
        evidence: Optional[List[str]] = None,
        is_explicit_provider_evidence: bool = False,
    ) -> Dict:
        """Update model credit status.

        Exhausted may be set only from explicit provider/account evidence.
        Operational failures (timeout, no_code, etc.) must leave credits unknown or unchanged.

        Args:
            registry: Current registry state
            model_id: Model identifier
            credit_status: New status (available/low/exhausted/unknown)
            evidence: Optional list of evidence strings for the transition
            is_explicit_provider_evidence: If True and status=exhausted, records evidence+tstamp

        Returns:
            Updated registry with credit state change persisted
        """
        errors = self.validate_credit_status(credit_status)
        if errors:
            raise ValueError("; ".join(errors))

        model = self.get_model(model_id, registry)
        if model is None:
            raise ValueError(f"Unknown model: {model_id}")

        # Validate exhausted only from explicit provider evidence
        if credit_status == "exhausted" and not is_explicit_provider_evidence:
            raise ValueError(
                "Credit status 'exhausted' may be set only from explicit provider/account "
                "evidence. Use is_explicit_provider_evidence=True with evidence for exhausted."
            )
        if credit_status == "exhausted" and (
            not evidence or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            raise ValueError("exhausted credit status requires non-empty provider/account evidence")

        # Store supplied registry for seeding if disk state is empty
        supplied_registry = copy.deepcopy(registry)

        # Build the mutation function
        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            # Persisted state is authoritative after initialization; the
            # supplied registry seeds a new state directory.
            result = current if current.get("models") else supplied_registry
            result = copy.deepcopy(result)

            # Find and update the model directly in result (don't use get_model
            # because it returns a copy)
            found = False
            for model in result.get("models", []):
                if model.get("id") == model_id:
                    model["credit_status"] = credit_status

                    # Record explicit provider credit exhaustion evidence and timestamp
                    if is_explicit_provider_evidence and credit_status == "exhausted":
                        model["credit_evidence"] = list(evidence) if evidence else []
                        model["credit_exhausted_at"] = self.state.timestamp_now()
                    elif credit_status == "exhausted":
                        # Clear exhaustion evidence/timestamp if set without explicit evidence
                        model.pop("credit_evidence", None)
                        model.pop("credit_exhausted_at", None)
                    found = True
                    break

            if not found:
                raise ValueError(f"Unknown model: {model_id}")

            # Preserve existing ledger entries
            return result, entries

        # Persist atomically via state.update_state
        self.state.update_state(mutate)
        # Return only the registry part, not the full tuple
        return self.state.get_registry()

    def recover_credit_status(
        self,
        registry: Dict,
        model_id: str,
        new_status: str,
        *,
        evidence: Optional[List[str]] = None,
    ) -> Dict:
        """Recover credit status to available/low/unknown from exhausted.

        Args:
            registry: Current registry state
            model_id: Model identifier
            new_status: Recovery status (available/low/unknown)
            evidence: Optional list of evidence strings for recovery

        Returns:
            Updated registry with recovery persisted
        """
        errors = self.validate_credit_status(new_status)
        if errors:
            raise ValueError("; ".join(errors))

        if new_status == "exhausted":
            raise ValueError("Recovery must be to available/low/unknown, not exhausted")

        model = self.get_model(model_id, registry)
        if model is None:
            raise ValueError(f"Unknown model: {model_id}")

        # Store supplied registry for seeding if disk state is empty
        supplied_registry = copy.deepcopy(registry)

        # Build the mutation function
        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            result = current if current.get("models") else supplied_registry
            result = copy.deepcopy(result)

            # Find and update the model directly in result (don't use get_model
            # because it returns a copy)
            found = False
            for model in result.get("models", []):
                if model.get("id") == model_id:
                    model["credit_status"] = new_status

                    # Clear exhaustion evidence/timestamp on recovery
                    model.pop("credit_evidence", None)
                    model.pop("credit_exhausted_at", None)

                    # Record recovery evidence if provided
                    if evidence:
                        model["credit_recovery_evidence"] = list(evidence)
                        model["credit_recovered_at"] = self.state.timestamp_now()

                    found = True
                    break

            if not found:
                raise ValueError(f"Unknown model: {model_id}")

            # Preserve existing ledger entries
            return result, entries

        # Persist atomically via state.update_state
        self.state.update_state(mutate)
        # Return only the registry part, not the full tuple
        return self.state.get_registry()

    def classify_as_substantive(self, classification: str) -> bool:
        return classification in self.SUBSTANTIVE_CLASSIFICATIONS

    def classify_as_non_substantive(self, classification: str) -> bool:
        return classification in self.NON_SUBSTANTIVE_CLASSIFICATIONS

    def is_explicit_credit_evidence_classification(self, classification: str) -> bool:
        return classification in self.EXPLICIT_CREDIT_EVIDENCE_CLASSIFICATIONS

    def get_substantive_failure_count(self, model_id: str, goal_class: str, registry: Dict) -> int:
        model = self.get_model(model_id, registry)
        return (model or {}).get("failure_counts_by_goal", {}).get(goal_class, 0)

    def increment_substantive_failure(self, model_id: str, goal_class: str, registry: Dict) -> Dict:
        from .registry import ModelRegistry
        result = copy.deepcopy(registry)
        registry_obj = ModelRegistry(self.state)
        return registry_obj.increment_substantive_failure(model_id, goal_class, result)

    def set_substantive_failure_count(self, first, second, third, fourth) -> Dict:
        """Persist a failure streak while accepting both legacy call orders.

        The P7.1 registry API used ``(model_id, goal_class, registry)`` while
        early P7.2 callers used ``(model_id, goal_class, count, registry)``.
        Keep the latter stable and also accept the reviewer's registry-first
        form so callers cannot silently update the wrong model.
        """
        if isinstance(first, dict):
            registry, model_id, goal_class, count = first, second, third, fourth
        else:
            model_id, goal_class, count, registry = first, second, third, fourth
        from .registry import ModelRegistry

        # Store supplied registry for seeding if disk state is empty
        supplied_registry = copy.deepcopy(registry)

        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            result = current if current.get("models") else supplied_registry
            result = copy.deepcopy(result)

            # Find and update the model directly in result (don't use get_model
            # because it returns a copy)
            found = False
            for model in result.get("models", []):
                if model.get("id") == model_id:
                    model.setdefault("failure_counts_by_goal", {})[goal_class] = count
                    found = True
                    break

            if not found:
                raise ValueError(f"Unknown model: {model_id}")

            # Preserve existing ledger entries
            return result, entries
        self.state.update_state(mutate)
        # Return only the registry part, not the full tuple
        return self.state.get_registry()
