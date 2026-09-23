"""P7.5 default coding dispatcher and reviewer chain.

Route coding to P40 first; after three substantive P40 failures for the goal,
select a healthy usable Second Shift member by task type; send every candidate
attempt to Codex for routine independent review; require a fresh Codex pass for
final verification. Copilot is a coding/repair worker, not the routine
reviewer. Persist
selected model, reviewer, attempt count, failure classification, credit status,
and final decision atomically using the existing StateFile transaction.

Treat no-code, timeout, provider/auth/rate-limit/refusal/context/credit events
as non-substantive and do not advance the three-code-failure escalation counter.

State namespace:
- ``coding_dispatcher`` registry: tracks P40 attempt counts and last decision
- ``model_attempts`` ledger: records every coding attempt with classification

Author: P7.5
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from .state import StateFile
from .workflow_policy import (
    DIRECT_ESCALATION,
    ROUTINE_REVIEWER,
    WorkflowName,
    choose_repair_worker,
    get_workflow_contract,
    provider_transition,
)

# Import GTX broker for deterministic routing (lazy import to avoid circular deps)
GTX_BROKER_AVAILABLE = False
GtxTaskClassifier = None  # Will be imported lazily
GtxBrokerDirector = None
RoutingRequest = None


def get_gtx_broker_director(state_file):
    """Load the standalone GTX broker package."""
    global GTX_BROKER_AVAILABLE, GtxBrokerDirector, RoutingRequest
    if GtxBrokerDirector is None:
        try:
            from gtx_broker.controller import (
                GtxBrokerDirector as _GtxBrokerDirector,
                RoutingRequest as _RoutingRequest,
            )
            GtxBrokerDirector = _GtxBrokerDirector
            RoutingRequest = _RoutingRequest
            GTX_BROKER_AVAILABLE = True
        except Exception as exc:
            GTX_BROKER_AVAILABLE = False
            raise RuntimeError(f"GTX broker import failed: {exc}") from exc
    return GtxBrokerDirector(state_file), RoutingRequest

def get_gtx_task_classifier():
    """Lazy import of the standalone GTX classifier."""
    global GTX_BROKER_AVAILABLE, GtxTaskClassifier
    if GtxTaskClassifier is None:
        try:
            from gtx_broker.classifier import GtxTaskClassifier
            GtxTaskClassifier = GtxTaskClassifier
            GTX_BROKER_AVAILABLE = True
        except Exception as e:
            print(f"GTX broker import failed: {e}")
            GTX_BROKER_AVAILABLE = False
    return GtxTaskClassifier


class ReviewOutcome(Enum):
    """Routine Codex review or final-verification outcomes."""
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


@dataclass
class DispatcherAttempt:
    """Record of a single coding attempt."""
    model_id: str
    provider: str
    goal_id: str
    goal_class: str
    classification: str
    credit_status: str
    attempt_number: int
    timestamp: str
    reviewed_by: Optional[str] = None
    review_outcome: Optional[str] = None
    reviewer_evidence: Optional[str] = None
    final_decision: Optional[str] = None  # "accepted", "rejected", "escalated"
    is_final_verification: bool = False
    attempt_role: str = "candidate"


class DefaultCodingDispatcher:
    """Default coding dispatcher and reviewer chain.

    P7.5 Scope:
    - Route coding to P40 first
    - After three substantive failures, select the workflow's configured
      repair/fallback worker
    - Send every candidate attempt to Codex for routine review
    - Require a fresh Codex pass for final verification
    - Persist selected model, reviewer, attempt count, failure classification,
      credit status, and final decision atomically
    """

    SUBSTANTIVE_FAILURE_THRESHOLD = 3

    SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "partial_code", "incorrect_code", "tests_failed",
    })

    # Non-substantive classifications that do NOT advance the failure counter
    NON_SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "no_code", "timeout", "endpoint_unavailable", "authentication",
        "credit_exhausted", "rate_limited", "policy_refusal", "context_limit",
        "review_unavailable", "unknown",
    })
    COPILOT_UNAVAILABLE_CLASSIFICATIONS = frozenset({
        "endpoint_unavailable", "authentication",
    })

    def __init__(self, state_file: StateFile):
        self.state = state_file

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _get_dispatcher_registry(self) -> Dict:
        """Load or seed the dispatcher registry."""
        registry = self.state.get_registry()
        if "coding_dispatcher" not in registry:
            registry["coding_dispatcher"] = {
                "p40_attempts_by_goal": {},
                "last_decision": None,
                "selected_model": None,
            }
        return registry

    def _get_attempts_ledger(self) -> List[Dict]:
        """Load the attempts ledger."""
        registry = self.state.get_registry()
        entries = self.state.get_ledger_entries()
        return [e for e in entries if e.get("ledger_type") == "model_attempts"]

    def _save_dispatcher_registry(self, registry: Dict) -> None:
        """Save the dispatcher registry."""
        self.state.save_registry(registry)

    def _append_attempt(self, attempt: Dict) -> None:
        """Append an attempt to the ledger."""
        attempt["ledger_type"] = "model_attempts"
        self.state.append_ledger_entry(attempt)

    def classify_as_substantive(self, classification: str) -> bool:
        """Check if classification counts as a substantive failure."""
        return classification in self.SUBSTANTIVE_CLASSIFICATIONS

    @staticmethod
    def _copilot_available(registry: Dict) -> bool:
        """Return whether Copilot is active and not explicitly out of credit."""
        model = next(
            (item for item in registry.get("models", []) if item.get("id") == "copilot"),
            None,
        )
        return bool(
            model
            and model.get("status") == "active"
            and model.get("credit_status", "unknown") != "exhausted"
        )

    def _route_repair_worker(
        self,
        registry: Dict,
        dispatcher: Dict,
        entries: List[Dict],
        *,
        goal_id: str,
    ) -> tuple[str, List[Dict]]:
        """Select Copilot or Codex repair and persist provider failover."""
        copilot_available = self._copilot_available(registry)
        selected_model = choose_repair_worker(copilot_available=copilot_available)
        if selected_model == "codex":
            copilot = next(
                (item for item in registry.get("models", []) if item.get("id") == "copilot"),
                None,
            )
            reason = (
                "quota_exhausted"
                if copilot and copilot.get("credit_status") == "exhausted"
                else "provider_unavailable"
            )
            transition = provider_transition(
                from_worker="copilot", to_worker="codex", reason=reason
            )
            record = {
                **transition,
                "goal_id": goal_id,
                "timestamp": self._now(),
                "ledger_type": "workflow_transitions",
            }
            dispatcher["last_provider_transition"] = record
            entries = entries + [record]
        return selected_model, entries

    def route_coding_request(
        self,
        goal_id: str,
        goal_class: str,
        registry: Dict,
        *,
        workflow: WorkflowName | str = WorkflowName.LAYERED_REVIEW,
        repair: bool = False,
        gtx_context_size: int = 65536,  # Default 64k context for GTX
        gtx_quality_importance: float = 0.7,  # Default quality-focused
        task_description: Optional[str] = None,
        requires_benchmark_evidence: bool = False,
    ) -> Dict:
        """Route a coding request to the appropriate model.

        Args:
            goal_id: Unique identifier for the coding goal/task
            goal_class: Classification of the goal (coding, reasoning, review, general)
            registry: Current registry state
            workflow: Layered Review, Direct Escalation, or gtx_direct_escalation
            repair: Route the repair worker instead of the normal coding path
            gtx_context_size: Context size for GTX broker routing (default 64k)
            gtx_quality_importance: Quality vs. speed preference for GTX (0.0-1.0)
            task_description: Full task prompt used for classification
            requires_benchmark_evidence: Force P40 benchmark-validation routing

        Returns:
            Updated registry with routing decision persisted
        """
        supplied_registry = copy.deepcopy(registry)

        # Handle GTX broker workflow separately (doesn't use workflow contracts)
        is_gtx_broker = workflow == "gtx_direct_escalation"

        # Stateful broker routing must happen outside the StateFile transaction;
        # GtxBrokerDirector persists its own durable state.
        gtx_routing_result = None
        if is_gtx_broker:
            try:
                director, request_type = get_gtx_broker_director(self.state)
                description = task_description or f"Task {goal_id} ({goal_class})"
                current_state = director.get_current_state()
                if current_state and current_state.task_id == goal_id:
                    # record_attempt_result already selected the next worker;
                    # do not evaluate the same failure twice on the next route.
                    gtx_routing_result = {
                        "selected_model": current_state.selected_model,
                        "model_config": director.classifier.get_model_constraints(current_state.selected_model),
                        "evidence": current_state.evidence,
                        "routing_timestamp": current_state.last_routing_at,
                        "classification": current_state.task_class,
                    }
                else:
                    broker_response = director.route_request(request_type(
                        task_id=goal_id,
                        task_description=description,
                        context_size=gtx_context_size,
                        quality_importance=gtx_quality_importance,
                        requires_benchmark_evidence=requires_benchmark_evidence,
                        workflow="gtx_direct_escalation",
                    ))
                    current_state = director.get_current_state()
                    gtx_routing_result = {
                        "selected_model": broker_response.target_worker,
                        "model_config": broker_response.model_config,
                        "evidence": broker_response.evidence,
                        "routing_timestamp": broker_response.routing_timestamp,
                        "classification": current_state.task_class if current_state else None,
                    }
            except Exception as e:
                gtx_routing_result = {
                    "selected_model": "p40",
                    "error": str(e),
                }

        if not is_gtx_broker:
            contract = get_workflow_contract(workflow)

        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            current = copy.deepcopy(current)
            # The model registry is a caller-supplied snapshot.  Preserve the
            # persisted dispatcher counters, but do not let an older model
            # snapshot hide a provider becoming exhausted or available.
            if "models" in supplied_registry:
                current["models"] = copy.deepcopy(supplied_registry["models"])
            elif not current.get("models") and supplied_registry:
                # Merge the caller snapshot without discarding durable broker
                # namespaces written before this dispatcher transaction.
                for key, value in supplied_registry.items():
                    current.setdefault(key, copy.deepcopy(value))

            dispatcher = current.setdefault("coding_dispatcher", {})
            dispatcher.setdefault("p40_substantive_failures_by_goal", {})

            # Start fresh attempt counter for this goal
            key = f"{goal_id}:{goal_class}"
            p40_failures = dispatcher.get("p40_substantive_failures_by_goal", {})

            # Check if P40 has exceeded substantive failure threshold
            p40_failure_count = p40_failures.get(key, 0)

            # A stateful GTX broker decision is authoritative.  In
            # particular, attempt_coding_task() sets repair=True for Codex
            # attempts; that must not let generic Copilot/Codex repair
            # selection replace an explicit broker escalation.
            if repair and not is_gtx_broker:
                selected_model, entries = self._route_repair_worker(
                    current, dispatcher, entries, goal_id=goal_id
                )
                dispatcher["repair_mode"] = True
            elif is_gtx_broker:
                # Use pre-computed GTX broker result (computed outside lock)
                if gtx_routing_result and "error" not in gtx_routing_result:
                    selected_model = gtx_routing_result["selected_model"]
                    dispatcher["gtx_broker_used"] = True
                    dispatcher["gtx_model_config"] = gtx_routing_result.get("model_config", {})
                else:
                    # Broker failed, fall back to P40
                    selected_model = "p40"
                    dispatcher["gtx_broker_used"] = False
                    if gtx_routing_result and "error" in gtx_routing_result:
                        dispatcher["gtx_broker_error"] = gtx_routing_result["error"]
                    dispatcher["repair_mode"] = False
            elif p40_failure_count < self.SUBSTANTIVE_FAILURE_THRESHOLD:
                # Route to P40
                selected_model = "p40"
                dispatcher["repair_mode"] = False
            else:
                if contract.name == DIRECT_ESCALATION.name:
                    selected_model, entries = self._route_repair_worker(
                        current, dispatcher, entries, goal_id=goal_id
                    )
                    dispatcher["repair_mode"] = True
                else:
                    # Layered Review retains the Second Shift fallback.
                    selected_model = self._select_secondary_model(goal_class, current)
                    dispatcher["repair_mode"] = False

            dispatcher["selected_model"] = selected_model
            dispatcher["last_decision"] = f"route_to_{selected_model}"
            dispatcher["last_routing_at"] = self._now()
            if is_gtx_broker:
                dispatcher["workflow"] = "gtx_direct_escalation"
                if gtx_routing_result:
                    dispatcher["gtx_broker_used"] = "error" not in gtx_routing_result
                    dispatcher["gtx_model_config"] = gtx_routing_result.get("model_config", {})
                    if "evidence" in gtx_routing_result:
                        decision_entry = {
                            "ledger_type": "gtx_broker_decision",
                            "goal_id": goal_id,
                            "target_worker": selected_model,
                            "model_config": gtx_routing_result["model_config"],
                            "evidence": gtx_routing_result["evidence"],
                            "classification": gtx_routing_result.get("classification"),
                            "routing_timestamp": gtx_routing_result.get("routing_timestamp"),
                        }
                        if not any(
                            entry.get("ledger_type") == "gtx_broker_decision"
                            and entry.get("goal_id") == goal_id
                            and entry.get("routing_timestamp") == decision_entry["routing_timestamp"]
                            for entry in entries
                        ):
                            entries.append(decision_entry)
                    if "error" in gtx_routing_result:
                        dispatcher["gtx_broker_error"] = gtx_routing_result["error"]
            else:
                dispatcher["workflow"] = contract.name.value if hasattr(contract, 'name') else str(workflow)

            return current, entries

        self.state.update_state(mutate)
        return self.state.get_registry()

    def record_attempt_result(
        self,
        *,
        goal_id: str,
        goal_class: str,
        model_id: str,
        provider: str,
        classification: str,
        credit_status: str,
        reviewer: Optional[str] = None,
        review_outcome: Optional[str] = None,
        reviewer_evidence: Optional[str] = None,
        final_decision: Optional[str] = None,
        is_final_verification: bool = False,
        attempt_role: str = "candidate",
        registry: Optional[Dict] = None,
        evidence: Optional[List[str]] = None,
        workflow: WorkflowName | str = WorkflowName.LAYERED_REVIEW,
    ) -> Dict:
        """Record the result of a coding attempt.

        Args:
            goal_id: Unique identifier for the coding goal
            goal_class: Classification of the goal
            model_id: Model that attempted the task
            provider: Provider used
            classification: Classification of the result
            credit_status: Credit status at time of attempt
            reviewer: Reviewer identity; substantive candidates require Codex
            review_outcome: Review result (approved/rejected/needs_revision)
            reviewer_evidence: Evidence for reviewer's decision
            final_decision: Final outcome (accepted/rejected/escalated)
            is_final_verification: True if this is a fresh Codex final verification
            attempt_role: candidate, repair, or final_verification
            registry: Current registry state
            evidence: List of evidence strings

        Returns:
            Updated registry state
        """
        supplied_registry = registry if registry else self.state.get_registry()
        timestamp = self._now()

        # Determine if this counts as a substantive failure
        is_substantive = self.classify_as_substantive(classification)

        if attempt_role not in {"candidate", "repair", "final_verification"}:
            raise ValueError("Unknown coding attempt role")
        if attempt_role == "final_verification" and not is_final_verification:
            raise ValueError("Final verification attempts must be marked explicitly")
        if model_id == "codex":
            if attempt_role == "final_verification":
                if reviewer != "codex":
                    raise ValueError("Codex final verification requires Codex reviewer")
            elif attempt_role != "repair" or is_final_verification:
                raise ValueError("Codex coding attempts must be explicit repair attempts")
            elif reviewer not in (None, ""):
                raise ValueError("Codex repair requires a separate fresh verification")
        elif attempt_role == "final_verification" or is_final_verification:
            raise ValueError("Only Codex may perform final verification")
        elif classification == "success" and reviewer != ROUTINE_REVIEWER:
            raise ValueError("successful candidate attempts require Codex review")
        elif is_substantive and reviewer != ROUTINE_REVIEWER:
            raise ValueError("substantive candidate attempts require Codex review")
        if model_id != "codex" and final_decision == "accepted":
            final_decision = "needs_codex_verification"
        if model_id == "codex" and attempt_role == "repair" and final_decision == "accepted":
            final_decision = "needs_fresh_codex_verification"

        is_gtx_workflow = workflow == "gtx_direct_escalation"
        gtx_controller_result = None
        if is_gtx_workflow:
            director, _ = get_gtx_broker_director(self.state)
            findings = list(evidence or [])
            if reviewer_evidence and reviewer_evidence not in findings:
                findings.append(reviewer_evidence)
            gtx_controller_result = director.record_attempt_result(
                task_id=goal_id,
                classification=classification,
                findings=findings,
                review_evidence=reviewer_evidence,
                is_architectural=classification in {"architectural", "complex_reasoning"},
                is_cross_file=any("cross-file" in item.lower() for item in findings),
                is_repeated=any(
                    "repeated" in item.lower() or "same conceptual error" in item.lower()
                    for item in findings
                ),
                workflow="direct_escalation",
            )

        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            current = copy.deepcopy(current)
            if not current.get("models") and supplied_registry:
                for key, value in supplied_registry.items():
                    current.setdefault(key, copy.deepcopy(value))

            # Update dispatcher state - only increment if P40 and substantive.
            dispatcher = current.setdefault("coding_dispatcher", {})
            dispatcher.setdefault("p40_substantive_failures_by_goal", {})
            key = f"{goal_id}:{goal_class}"

            if model_id in {"p40", "p40_qwen35"} and is_substantive:
                current_failures = dispatcher["p40_substantive_failures_by_goal"].get(key, 0)
                dispatcher["p40_substantive_failures_by_goal"][key] = current_failures + 1
            else:
                # Non-substantive or secondary model - don't increment P40 counter
                pass

            if model_id == "copilot":
                copilot = next(
                    (item for item in current.get("models", [])
                     if item.get("id") == "copilot"),
                    None,
                )
                if copilot is not None:
                    provider_evidence = list(evidence) if evidence else []
                    if reviewer_evidence and reviewer_evidence not in provider_evidence:
                        provider_evidence.append(reviewer_evidence)
                    if not provider_evidence:
                        provider_evidence = [f"{provider} reported {classification}"]

                    if classification == "credit_exhausted":
                        copilot["credit_status"] = "exhausted"
                        copilot["credit_evidence"] = provider_evidence
                        copilot["credit_exhausted_at"] = timestamp
                    elif classification in self.COPILOT_UNAVAILABLE_CLASSIFICATIONS:
                        # Provider availability is separate from credit state:
                        # an endpoint/auth failure must not be misreported as
                        # exhausted credit, but it must affect the next route.
                        copilot["status"] = "unusable"
                        copilot["quarantine_reason"] = (
                            f"Copilot provider unavailable: {classification}"
                        )
                        copilot["provider_unavailable_evidence"] = provider_evidence
                        copilot["provider_unavailable_at"] = timestamp

            attempt_number = 1 + sum(
                1 for entry in entries
                if entry.get("ledger_type") == "model_attempts"
                and entry.get("goal_id") == goal_id
                and entry.get("goal_class") == goal_class
            )
            attempt_record = {
                "goal_id": goal_id,
                "goal_class": goal_class,
                "model_id": model_id,
                "provider": provider,
                "classification": classification,
                "credit_status": credit_status,
                "is_substantive_failure": is_substantive,
                "attempt_number": attempt_number,
                "reviewer": reviewer,
                "review_outcome": review_outcome,
                "reviewer_evidence": reviewer_evidence,
                "final_decision": final_decision,
                "is_final_verification": is_final_verification,
                "attempt_role": attempt_role,
                "timestamp": timestamp,
                "evidence": list(evidence) if evidence else [],
                "ledger_type": "model_attempts",
            }
            dispatcher["selected_model"] = model_id
            dispatcher["last_decision"] = final_decision or "attempt_in_progress"
            dispatcher["last_attempt_at"] = timestamp
            if gtx_controller_result is not None:
                dispatcher["workflow"] = "gtx_direct_escalation"
                dispatcher["gtx_broker_used"] = True
                dispatcher["gtx_model_config"] = gtx_controller_result.model_config
                dispatcher["selected_model"] = gtx_controller_result.target_worker
                dispatcher["last_decision"] = gtx_controller_result.decision.value
                dispatcher["gtx_attempt_number"] = gtx_controller_result.attempt_number
                dispatcher["gtx_failure_counts"] = {
                    "gtx": gtx_controller_result.gtx_failures,
                    "p40": gtx_controller_result.p40_failures,
                }

            return current, entries + [attempt_record]

        return self.state.update_state(mutate)

    def record_provider_transition(
        self,
        *,
        goal_id: str,
        from_worker: str,
        to_worker: str,
        reason: str,
        registry: Optional[Dict] = None,
    ) -> Dict:
        """Persist a provider failover without counting an implementation failure."""
        transition = provider_transition(
            from_worker=from_worker, to_worker=to_worker, reason=reason
        )
        supplied_registry = registry if registry else self.state.get_registry()
        timestamp = self._now()

        def mutate(current: Dict, entries: List[Dict]) -> tuple[Dict, List[Dict]]:
            current = current if current.get("models") else supplied_registry
            current = copy.deepcopy(current)
            record = {
                **transition,
                "goal_id": goal_id,
                "timestamp": timestamp,
                "ledger_type": "workflow_transitions",
            }
            current.setdefault("coding_dispatcher", {})[
                "last_provider_transition"
            ] = record
            return current, entries + [record]

        return self.state.update_state(mutate)

    def _select_secondary_model(
        self,
        goal_class: str,
        registry: Dict,
    ) -> str:
        """Select a healthy usable Second Shift member by task type.

        Uses get_usable_member_ids() to find active, non-exhausted models,
        then selects the one with lowest failure count for the goal_class.
        """
        from .pool_recovery import get_usable_member_ids

        usable_ids = get_usable_member_ids(registry)

        # Remove "p40" from consideration if accidentally included
        usable_ids.difference_update({"p40", "copilot", "codex"})

        if not usable_ids:
            raise ValueError(
                "No usable Second Shift members available after P40 exhaustion"
            )

        # Select model with lowest failure count for this goal class
        from .registry import ModelRegistry
        registry_obj = ModelRegistry(self.state)

        best_model = None
        best_count = float("inf")

        for model_id in usable_ids:
            count = registry_obj.get_substantive_failure_count(
                model_id, goal_class, registry
            )
            if count < best_count:
                best_count = count
                best_model = model_id

        if best_model is None:
            # Fallback to first available
            best_model = sorted(usable_ids)[0]

        return best_model

    def attempt_coding_task(
        self,
        *,
        goal_id: str,
        goal_class: str,
        model_id: str,
        provider: str,
        classification: str,
        credit_status: str,
        reviewer: Optional[str],
        review_outcome: str,
        reviewer_evidence: str,
        evidence: List[str],
        registry: Optional[Dict] = None,
        workflow: WorkflowName | str = WorkflowName.LAYERED_REVIEW,
        repair: bool = False,
        task_description: Optional[str] = None,
        gtx_context_size: int = 65536,
        gtx_quality_importance: float = 0.7,
        requires_benchmark_evidence: bool = False,
    ) -> Dict:
        """Execute the full coding task workflow.

        This is the main entry point for a coding attempt:
        1. Route to P40 or secondary model
        2. Record attempt with classification
        3. Send the candidate to Codex for routine review
        4. If accepted, run a fresh Codex final-verification pass
        5. Persist all decisions atomically

        Args:
            goal_id: Unique identifier for the coding goal
            goal_class: Classification of the goal
            model_id: Model that attempted the task
            provider: Provider used
            classification: Classification of the result
            credit_status: Credit status at time of attempt
            reviewer: Routine reviewer identity; use Codex for candidate review
            review_outcome: Review result
            reviewer_evidence: Evidence for reviewer's decision
            evidence: List of evidence strings
            registry: Current registry state

        Returns:
            Updated registry and attempt decision
        """
        registry = registry if registry else self.state.get_registry()

        # Route the request
        is_repair = repair or model_id == "codex"
        registry = self.route_coding_request(
            goal_id,
            goal_class,
            registry,
            workflow=workflow,
            repair=is_repair,
            task_description=task_description,
            gtx_context_size=gtx_context_size,
            gtx_quality_importance=gtx_quality_importance,
            requires_benchmark_evidence=requires_benchmark_evidence,
        )
        selected_model = registry.get("coding_dispatcher", {}).get("selected_model")
        if selected_model != model_id:
            raise ValueError(
                f"coding attempt model {model_id!r} does not match routed model "
                f"{selected_model!r}"
            )
        attempt_role = "repair" if is_repair else "candidate"
        final_decision = "rejected"
        if review_outcome == "approved":
            final_decision = (
                "needs_fresh_codex_verification"
                if model_id == "codex"
                else "needs_codex_verification"
            )

        # Record the candidate attempt. Codex review still requires the
        # explicit fresh Codex final-verification call below.
        registry = self.record_attempt_result(
            goal_id=goal_id,
            goal_class=goal_class,
            model_id=model_id,
            provider=provider,
            classification=classification,
            credit_status=credit_status,
            reviewer=reviewer,
            review_outcome=review_outcome,
            reviewer_evidence=reviewer_evidence,
            final_decision=final_decision,
            is_final_verification=False,
            attempt_role=attempt_role,
            registry=registry,
            evidence=evidence,
            workflow=workflow,
        )

        return registry

    def record_final_verification(
        self,
        *,
        goal_id: str,
        goal_class: str,
        provider: str,
        classification: str = "success",
        credit_status: str = "unknown",
        review_outcome: str = "approved",
        reviewer_evidence: str,
        evidence: List[str],
        registry: Optional[Dict] = None,
        workflow: WorkflowName | str = WorkflowName.LAYERED_REVIEW,
    ) -> Dict:
        """Persist the required Codex final-verification decision."""
        return self.record_attempt_result(
            goal_id=goal_id,
            goal_class=goal_class,
            model_id="codex",
            provider=provider,
            classification=classification,
            credit_status=credit_status,
            reviewer="codex",
            review_outcome=review_outcome,
            reviewer_evidence=reviewer_evidence,
            final_decision="accepted" if review_outcome == "approved" else "rejected",
            is_final_verification=True,
            attempt_role="final_verification",
            registry=registry,
            evidence=evidence,
            workflow=workflow,
        )

    def get_dispatcher_state(self, registry: Optional[Dict] = None) -> Dict:
        """Get the current dispatcher state.

        Args:
            registry: Current registry state (loads from disk if None)

        Returns:
            Dispatcher state with p40 failures, selected model, and decisions
        """
        registry = registry if registry else self.state.get_registry()
        dispatcher = registry.get("coding_dispatcher", {})
        return {
            "p40_substantive_failures_by_goal": dispatcher.get("p40_substantive_failures_by_goal", {}),
            "selected_model": dispatcher.get("selected_model"),
            "workflow": dispatcher.get("workflow"),
            "repair_mode": dispatcher.get("repair_mode", False),
            "last_provider_transition": dispatcher.get("last_provider_transition"),
            "last_decision": dispatcher.get("last_decision"),
            "last_routing_at": dispatcher.get("last_routing_at"),
            "last_attempt_at": dispatcher.get("last_attempt_at"),
        }

    def get_attempt_history(
        self,
        goal_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> List[Dict]:
        """Get attempt history for a goal or model.

        Args:
            goal_id: Filter by goal (optional)
            model_id: Filter by model (optional)

        Returns:
            List of attempt records
        """
        entries = self._get_attempts_ledger()

        if goal_id:
            entries = [e for e in entries if e.get("goal_id") == goal_id]
        if model_id:
            entries = [e for e in entries if e.get("model_id") == model_id]

        return entries

    def get_p40_failure_count(self, goal_id: str, goal_class: str) -> int:
        """Get the current P40 failure count for a goal.

        Args:
            goal_id: Unique identifier for the coding goal
            goal_class: Classification of the goal

        Returns:
            Number of P40 substantive failures for this goal
        """
        registry = self.state.get_registry()
        dispatcher = registry.get("coding_dispatcher", {})
        key = f"{goal_id}:{goal_class}"
        return dispatcher.get("p40_substantive_failures_by_goal", {}).get(key, 0)

    def can_route_to_secondary(self, goal_id: str, goal_class: str) -> bool:
        """Check if routing to secondary model is allowed.

        Args:
            goal_id: Unique identifier for the coding goal
            goal_class: Classification of the goal

        Returns:
            True if P40 has exceeded the failure threshold
        """
        count = self.get_p40_failure_count(goal_id, goal_class)
        return count >= self.SUBSTANTIVE_FAILURE_THRESHOLD
