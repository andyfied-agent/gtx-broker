"""GTX broker deterministic controller.

Main entry point for the gtx-broker-direct-escalation workflow.
Routes tasks to appropriate GPU/model based on classification and
escalation policy decisions.

Author: GTX Broker P7.5
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Callable
from enum import Enum

from .state import (
    GTXBrokerState,
    GTXBrokerRegistry,
    GTXBrokerWorkflow,
    ProviderTransition,
)
from .classifier import GtxTaskClassifier, TaskClassification
from .escalation_policy import (
    GtxEscalationPolicy,
    EscalationResult,
    RoutingDecision,
    FailureClassification,
)


class BrokerStatus(Enum):
    """Status of the broker controller."""
    IDLE = "idle"
    PROCESSING = "processing"
    ERROR = "error"


@dataclass
class RoutingRequest:
    """Input request to the broker."""
    task_id: str
    task_description: str
    context_size: int
    quality_importance: float = 0.5
    requires_benchmark_evidence: bool = False
    workflow: str = "direct_escalation"

    # For retry/repair scenarios
    previous_classification: Optional[str] = None
    previous_findings: Optional[List[str]] = None
    previous_review_evidence: Optional[str] = None
    previous_attempt_number: int = 0
    previous_gtx_failures: int = 0
    previous_p40_failures: int = 0


@dataclass
class RoutingResponse:
    """Output response from the broker."""
    task_id: str
    decision: RoutingDecision
    target_worker: str
    model_config: Dict[str, Any]
    reason: str
    evidence: List[str]
    requires_verification: bool = False
    attempt_number: int = 1
    gtx_failures: int = 0
    p40_failures: int = 0
    routing_timestamp: str = ""
    workflow: str = "direct_escalation"


class GtxBrokerDirector:
    """Deterministic controller for GTX broker.

    Main entry point that:
    1. Receives routing request
    2. Classifies task requirements
    3. Applies escalation policy
    4. Routes to appropriate worker (GTX/P40/Codex)
    5. Persists decision and transition evidence
    6. Returns routing response
    """

    def __init__(self, state_file):
        """Initialize broker with state file.

        Args:
            state_file: StateFile instance from second_shift.state
        """
        self.state_file = state_file
        self.registry = GTXBrokerRegistry(state_file)
        self.classifier = GtxTaskClassifier()
        self.escalation_policy = GtxEscalationPolicy()
        self.status = BrokerStatus.IDLE

    def _now(self) -> str:
        """Return current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def route_request(self, request: RoutingRequest) -> RoutingResponse:
        """Route a request to the appropriate worker.

        Args:
            request: Routing request with task details

        Returns:
            RoutingResponse with decision and target worker
        """
        self.status = BrokerStatus.PROCESSING

        try:
            # The dispatcher uses a descriptive public workflow name while
            # the policy/state enum stores the canonical value.
            policy_workflow = (
                "direct_escalation"
                if request.workflow == "gtx_direct_escalation"
                else request.workflow
            )
            # Step 1: Determine task classification and initial routing
            state = self.classifier.classify_task(
                task_id=request.task_id,
                task_description=request.task_description,
                context_size=request.context_size,
                quality_importance=request.quality_importance,
                requires_benchmark_evidence=request.requires_benchmark_evidence,
            )

            # Step 2: Apply escalation policy if this is a retry
            if (
                request.previous_classification
                and request.previous_attempt_number > 0
            ):
                escalation_result = self.escalation_policy.evaluate_failure(
                    classification=request.previous_classification,
                    findings=request.previous_findings or [],
                    review_evidence=request.previous_review_evidence,
                    is_architectural=False,  # Would be set by reviewer
                    is_cross_file=False,
                    is_repeated=False,
                    attempt_number=request.previous_attempt_number,
                    gtx_failures=request.previous_gtx_failures,
                    p40_failures=request.previous_p40_failures,
                    workflow=policy_workflow,
                )

                # Update state with escalation decision
                state.selected_model = escalation_result.target_worker
                state.gtx_substantive_failures = escalation_result.gtx_failures
                state.p40_substantive_failures = escalation_result.p40_failures
                state.attempt_number = request.previous_attempt_number + 1
                state.evidence.append(f"escalation_reason={escalation_result.reason}")
            else:
                # First attempt
                state.workflow = GTXBrokerWorkflow(policy_workflow)
                state.attempt_number = 1

            # Step 3: Set routing timestamp
            state.last_routing_at = self._now()

            # Step 4: Persist state
            self.registry.set_current_state(state)
            self.registry.append_task_history(state)

            # Step 5: Build response
            model_config = self.classifier.get_model_constraints(state.selected_model)

            response = RoutingResponse(
                task_id=request.task_id,
                decision=self._map_worker_to_decision(state.selected_model),
                target_worker=state.selected_model,
                model_config=model_config,
                reason=self._generate_reason(state, request),
                evidence=state.evidence,
                requires_verification=state.selected_model == "codex",
                attempt_number=state.attempt_number,
                gtx_failures=state.gtx_substantive_failures,
                p40_failures=state.p40_substantive_failures,
                routing_timestamp=state.last_routing_at,
                workflow=request.workflow,
            )

            self.status = BrokerStatus.IDLE
            return response

        except Exception as e:
            self.status = BrokerStatus.ERROR
            raise

    def record_attempt_result(
        self,
        *,
        task_id: str,
        classification: str,
        findings: List[str],
        review_evidence: Optional[str] = None,
        is_architectural: bool = False,
        is_cross_file: bool = False,
        is_repeated: bool = False,
        workflow: str = "direct_escalation",
    ) -> RoutingResponse:
        """Record result of an attempt and determine next routing.

        Args:
            task_id: Task identifier
            classification: Result classification
            findings: Review findings
            review_evidence: Codex review text
            is_architectural: Whether failure is architectural
            is_cross_file: Whether failure spans multiple files
            is_repeated: Whether same error repeated
            workflow: Workflow name

        Returns:
            RoutingResponse with next routing decision
        """
        # Get current state
        current_state = self.registry.get_task_state(task_id)
        if not current_state:
            raise ValueError(f"No state found for task {task_id}")

        # The current state represents the completed attempt. The next
        # attempt number is evaluated before any escalation decision.
        next_attempt_number = current_state.attempt_number + 1
        previous_worker = current_state.selected_model
        is_substantive = classification in GtxEscalationPolicy.SUBSTANTIVE_CLASSIFICATIONS
        gtx_failures = current_state.gtx_substantive_failures
        p40_failures = current_state.p40_substantive_failures
        if is_substantive:
            if previous_worker.startswith("gtx_"):
                gtx_failures += 1
            elif previous_worker == "p40_qwen35":
                p40_failures += 1

        # Evaluate escalation policy with updated attempt number
        escalation_result = self.escalation_policy.evaluate_failure(
            classification=classification,
            findings=findings,
            review_evidence=review_evidence,
            is_architectural=is_architectural,
            is_cross_file=is_cross_file,
            is_repeated=is_repeated,
            attempt_number=next_attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
            workflow=workflow,
        )

        # Update state
        current_state.selected_model = escalation_result.target_worker
        current_state.gtx_substantive_failures = gtx_failures
        current_state.p40_substantive_failures = p40_failures

        current_state.attempt_number = next_attempt_number
        current_state.evidence.append(f"result_classification={classification}")
        current_state.evidence.extend(findings)
        current_state.last_routing_at = self._now()

        # Add provider transition if needed
        if escalation_result.target_worker != previous_worker:
            transition = ProviderTransition(
                from_worker=previous_worker,
                to_worker=escalation_result.target_worker,
                reason=escalation_result.reason,
                timestamp=current_state.last_routing_at,
                evidence=escalation_result.evidence,
            )
            self.registry.append_provider_transition(transition)

        # Persist
        self.registry.set_current_state(current_state)
        self.registry.append_task_history(current_state)

        # Build response
        model_config = self.classifier.get_model_constraints(escalation_result.target_worker)

        response = RoutingResponse(
            task_id=task_id,
            decision=escalation_result.decision,
            target_worker=escalation_result.target_worker,
            model_config=model_config,
            reason=escalation_result.reason,
            evidence=escalation_result.evidence,
            requires_verification=escalation_result.requires_fresh_verification,
            attempt_number=current_state.attempt_number,
            gtx_failures=current_state.gtx_substantive_failures,
            p40_failures=current_state.p40_substantive_failures,
            routing_timestamp=current_state.last_routing_at,
            workflow=workflow,
        )

        return response

    def get_current_state(self) -> Optional[GTXBrokerState]:
        """Get the current broker state."""
        return self.registry.get_current_state()

    def get_task_history(self) -> List[Dict]:
        """Get the task history."""
        return self.registry.get_task_history()

    def get_settings(self) -> Dict:
        """Get broker settings."""
        return self.registry.get_settings()

    def update_settings(self, updates: Dict[str, Any]) -> Dict:
        """Update broker settings."""
        return self.registry.update_settings(updates)

    def _map_worker_to_decision(self, worker: str) -> RoutingDecision:
        """Map worker name to routing decision enum."""
        mapping = {
            "gtx_iq3_xs": RoutingDecision.RETRY_LOCAL,
            "gtx_q2_k": RoutingDecision.RETRY_LOCAL,
            "p40_qwen35": RoutingDecision.ESCALATE_TO_P40,
            "codex": RoutingDecision.ESCALATE_TO_CODEX,
            "second_shift": RoutingDecision.ESCALATE_TO_SECOND_SHIFT,
        }
        return mapping.get(worker, RoutingDecision.RETRY_LOCAL)

    def _generate_reason(self, state: GTXBrokerState, request: RoutingRequest) -> str:
        """Generate human-readable routing reason."""
        if request.previous_classification:
            return f"Routing based on previous classification: {request.previous_classification}"

        # Generate based on task characteristics
        reasons = []

        if state.task_class == "benchmark_validation":
            reasons.append("requires benchmark evidence")
        elif state.task_class == "large_context":
            reasons.append(f"context_size={state.context_size} exceeds GTX limit")
        elif state.task_class == "complex_reasoning":
            reasons.append("requires architectural reasoning")
        elif state.quality_preference == "quality":
            reasons.append("quality-focused routing")
        else:
            reasons.append("speed-focused routing")

        return f"Task classification: {state.task_class}; " + "; ".join(reasons)

    def reset_task_state(self, task_id: str) -> bool:
        """Reset state for a task (useful for testing/cancelled tasks)."""
        current_state = self.registry.get_current_state()
        if not current_state or current_state.task_id != task_id:
            return False

        current_state.gtx_substantive_failures = 0
        current_state.p40_substantive_failures = 0
        current_state.attempt_number = 1
        current_state.last_routing_at = self._now()

        self.registry.set_current_state(current_state)
        return True


__all__ = [
    "BrokerStatus",
    "RoutingRequest",
    "RoutingResponse",
    "GtxBrokerDirector",
]
