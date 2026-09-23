"""Escalation policy engine for GTX broker.

Implements deterministic escalation rules from direct-escalation.md:
- Retry locally: narrow concrete findings (missed assertion, edge case)
- Escalate to Codex: architectural misunderstanding, cross-file breaks
- Escalate to P40: task exceeds GTX scope, quality-critical
- Blocked: required evidence unavailable

Author: GTX Broker P7.5
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Any


class FailureClassification(Enum):
    """Classification of implementation failures."""
    # Substantive failures (count against attempt limit)
    PARTIAL_CODE = "partial_code"
    INCORRECT_CODE = "incorrect_code"
    TESTS_FAILED = "tests_failed"

    # Non-substantive (don't count against substantive failure counters, but
    # do consume the bounded operational retry budget.)
    NO_CODE = "no_code"
    TIMEOUT = "timeout"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    AUTHENTICATION = "authentication"
    CREDIT_EXHAUSTED = "credit_exhausted"
    RATE_LIMITED = "rate_limited"
    POLICY_REFUSAL = "policy_refusal"
    CONTEXT_LIMIT = "context_limit"
    UNKNOWN = "unknown"


class RoutingDecision(Enum):
    """Routing decisions for failure handling."""
    ACCEPT = "accept"
    ACCEPT_NEEDS_VERIFICATION = "accept_needs_verification"
    REJECT = "reject"
    RETRY_LOCAL = "retry_local"
    ESCALATE_TO_CODEX = "escalate_to_codex"
    ESCALATE_TO_P40 = "escalate_to_p40"
    ESCALATE_TO_SECOND_SHIFT = "escalate_to_second_shift"
    BLOCKED = "blocked"


@dataclass
class FailureEvidence:
    """Evidence for a failure classification."""
    classification: FailureClassification
    findings: List[str]  # Specific file/line findings
    reason: str
    is_architectural: bool = False
    is_cross_file: bool = False
    is_repeated: bool = False


@dataclass
class EscalationResult:
    """Result of escalation policy evaluation."""
    decision: RoutingDecision
    target_worker: str
    reason: str
    evidence: List[str]
    requires_fresh_verification: bool = False

    # Metadata
    attempt_number: int = 1
    gtx_failures: int = 0
    p40_failures: int = 0


class GtxEscalationPolicy:
    """Deterministic escalation policy engine.

    Encodes the escalation rules from direct-escalation.md as a decision tree.
    Evaluates failure evidence and returns routing decisions.
    """

    # Thresholds from review-and-retry.md
    MAX_GTX_ATTEMPTS = 3
    MAX_P40_ATTEMPTS = 3

    # Substantive failure classifications
    SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "partial_code", "incorrect_code", "tests_failed",
    })

    # Non-substantive classifications
    NON_SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "no_code", "timeout", "endpoint_unavailable", "authentication",
        "credit_exhausted", "rate_limited", "policy_refusal", "context_limit",
        "review_unavailable",
        "unknown",
    })

    # Escalation triggers from direct-escalation.md
    ESCALATION_TRIGGERS = frozenset({
        "same_conceptual_error_survives_correction",
        "misunderstands_ownership_authority",
        "requires_coordinated_schema_update",
        "solves_test_instead_of_acceptance_criterion",
        "unrelated_files_repeatedly_changed",
        "requires_weakening_existing_assertions",
        "misunderstands_runtime_safety_boundary",
    })

    def __init__(self):
        """Initialize escalation policy engine."""
        pass

    def evaluate_failure(
        self,
        *,
        classification: str,
        findings: List[str],
        review_evidence: Optional[str] = None,
        is_architectural: bool = False,
        is_cross_file: bool = False,
        is_repeated: bool = False,
        attempt_number: int,
        gtx_failures: int,
        p40_failures: int,
        workflow: str = "direct_escalation",
    ) -> EscalationResult:
        """Evaluate failure evidence and return routing decision."""
        # Handle acceptance conditions FIRST (before substantive check)
        if classification == "success":
            return self._handle_acceptance(
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )

        # Step 1: Determine if failure is substantive
        is_substantive = classification in self.SUBSTANTIVE_CLASSIFICATIONS

        # Step 2: Check for non-substantive conditions
        if not is_substantive:
            return self._handle_non_substantive(
                classification=classification,
                findings=findings,
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
                workflow=workflow,
            )

        # Step 3: Check for acceptance conditions
        if classification == "success":
            return self._handle_acceptance(
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )

        # Step 4: Evaluate escalation triggers
        escalation_reason = self._check_escalation_triggers(
            classification=classification,
            findings=findings,
            review_evidence=review_evidence,
            is_architectural=is_architectural,
            is_cross_file=is_cross_file,
            is_repeated=is_repeated,
        )

        # Step 5: Apply escalation policy
        if escalation_reason:
            return self._handle_escalation(
                reason=escalation_reason,
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
                workflow=workflow,
            )

        # Step 6: Default to retry-local for bounded corrections
        return self._handle_retry_local(
            attempt_number=attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
            workflow=workflow,
        )

    def _handle_non_substantive(
        self,
        classification: str,
        findings: List[str],
        attempt_number: int,
        gtx_failures: int,
        p40_failures: int,
        workflow: str,
    ) -> EscalationResult:
        """Handle operational failures with a bounded retry budget."""
        if attempt_number > self.MAX_P40_ATTEMPTS:
            target = "second_shift" if workflow == "layered_review" else "codex"
            decision = (
                RoutingDecision.ESCALATE_TO_SECOND_SHIFT
                if target == "second_shift"
                else RoutingDecision.ESCALATE_TO_CODEX
            )
            return EscalationResult(
                decision=decision,
                target_worker=target,
                reason=(
                    f"Exceeded non-substantive P40 retry limit "
                    f"({self.MAX_P40_ATTEMPTS}) after {classification}"
                ),
                evidence=[
                    f"classification={classification}",
                    f"attempt={attempt_number}",
                    f"limit={self.MAX_P40_ATTEMPTS}",
                ],
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )
        return EscalationResult(
            decision=RoutingDecision.RETRY_LOCAL,
            target_worker="p40_qwen35",
            reason=f"Non-substantive failure: {classification}",
            evidence=[f"classification={classification}"],
            attempt_number=attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
        )

    def _handle_acceptance(
        self,
        attempt_number: int,
        gtx_failures: int,
        p40_failures: int,
    ) -> EscalationResult:
        """Handle successful completion."""
        return EscalationResult(
            decision=RoutingDecision.ACCEPT_NEEDS_VERIFICATION,
            target_worker="codex",
            reason="Implementation accepted, requires Codex verification",
            evidence=["classification=success"],
            requires_fresh_verification=True,
            attempt_number=attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
        )

    def _check_escalation_triggers(
        self,
        classification: str,
        findings: List[str],
        review_evidence: Optional[str],
        is_architectural: bool,
        is_cross_file: bool,
        is_repeated: bool,
    ) -> Optional[str]:
        """Check if escalation triggers are present."""
        review_text = (review_evidence or "").lower()

        # Direct escalation criteria from direct-escalation.md
        if is_architectural:
            return "architectural_misunderstanding"

        if is_cross_file:
            return "cross_file_contract_breakage"

        if is_repeated:
            return "repeated_conceptual_error"

        # Keyword-based escalation triggers
        if self._find_keyword(review_text, [
            "architectural", "misunderstand", "wrong design",
            "fundamental error", "root cause", "deeply flawed"
        ]):
            return "architectural_misunderstanding"

        if self._find_keyword(review_text, [
            "multiple files", "cross-file", "contract break",
            "interface mismatch", "API change", "schema drift"
        ]):
            return "cross_file_contract_breakage"

        if self._find_keyword(review_text, [
            "same error", "repeated", "again", "still",
            "recurring", "persistent"
        ]):
            return "repeated_conceptual_error"

        # Test vs. acceptance criterion mismatch
        if self._find_keyword(review_text, [
            "solves test", "passes test", "test passes",
            "doesn't meet requirement", "acceptance criterion"
        ]):
            return "solves_test_instead_of_acceptance_criterion"

        # Repeated unrelated changes
        if len(findings) > 5:  # Many files changed
            return "unrelated_files_repeatedly_changed"

        return None

    def _handle_escalation(
        self,
        reason: str,
        attempt_number: int,
        gtx_failures: int,
        p40_failures: int,
        workflow: str,
    ) -> EscalationResult:
        """Handle escalation based on reason."""
        # Escalate directly to Codex for architectural/cross-file issues
        if reason in {"architectural_misunderstanding", "cross_file_contract_breakage"}:
            return EscalationResult(
                decision=RoutingDecision.ESCALATE_TO_CODEX,
                target_worker="codex",
                reason=f"Escalate to Codex: {reason}",
                evidence=[f"reason={reason}", f"attempt={attempt_number}"],
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )

        # Escalate to P40 if task exceeds GTX scope
        if reason in {"repeated_conceptual_error", "solves_test_instead_of_acceptance_criterion"}:
            return EscalationResult(
                decision=RoutingDecision.ESCALATE_TO_P40,
                target_worker="p40_qwen35",
                reason=f"Escalate to P40: {reason}",
                evidence=[f"reason={reason}", f"attempt={attempt_number}"],
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )

        # Layered Review fallback to Second Shift
        if workflow == "layered_review":
            return EscalationResult(
                decision=RoutingDecision.ESCALATE_TO_SECOND_SHIFT,
                target_worker="second_shift",
                reason=f"Escalate to Second Shift: {reason}",
                evidence=[f"reason={reason}", f"attempt={attempt_number}"],
                attempt_number=attempt_number,
                gtx_failures=gtx_failures,
                p40_failures=p40_failures,
            )

        # Default for Direct Escalation: escalate to Codex
        return EscalationResult(
            decision=RoutingDecision.ESCALATE_TO_CODEX,
            target_worker="codex",
            reason=f"Escalate to Codex: {reason}",
            evidence=[f"reason={reason}", f"attempt={attempt_number}"],
            attempt_number=attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
        )

    def _handle_retry_local(
        self,
        attempt_number: int,
        gtx_failures: int,
        p40_failures: int,
        workflow: str,
    ) -> EscalationResult:
        """Handle retry-local case (bounded correction)."""
        # Check if we've exceeded the attempt limit
        if workflow == "direct_escalation":
            # ``attempt_number`` is the next attempt to run. Allow the third
            # substantive attempt; escalate only after it has failed.
            if attempt_number > self.MAX_P40_ATTEMPTS:
                return EscalationResult(
                    decision=RoutingDecision.ESCALATE_TO_CODEX,
                    target_worker="codex",
                    reason=f"Exceeded P40 attempt limit ({self.MAX_P40_ATTEMPTS})",
                    evidence=[f"attempt={attempt_number}", f"limit={self.MAX_P40_ATTEMPTS}"],
                    attempt_number=attempt_number,
                    gtx_failures=gtx_failures,
                    p40_failures=p40_failures,
                )
        else:
            # Layered Review: check both GTX and P40 limits
            if gtx_failures >= self.MAX_GTX_ATTEMPTS:
                return EscalationResult(
                    decision=RoutingDecision.ESCALATE_TO_SECOND_SHIFT,
                    target_worker="second_shift",
                    reason=f"Exceeded GTX attempt limit ({self.MAX_GTX_ATTEMPTS})",
                    evidence=[f"gtx_failures={gtx_failures}", f"limit={self.MAX_GTX_ATTEMPTS}"],
                    attempt_number=attempt_number,
                    gtx_failures=gtx_failures,
                    p40_failures=p40_failures,
                )

        return EscalationResult(
            decision=RoutingDecision.RETRY_LOCAL,
            target_worker="p40_qwen35",
            reason="Retry local with bounded correction",
            evidence=[f"attempt={attempt_number}"],
            attempt_number=attempt_number,
            gtx_failures=gtx_failures,
            p40_failures=p40_failures,
        )

    def _find_keyword(self, text: str, keywords: list) -> bool:
        """Check if text contains any of the keywords."""
        text_lower = text.lower()
        for keyword in keywords:
            if keyword in text_lower:
                return True
        return False

    def is_substantive_failure(self, classification: str) -> bool:
        """Check if a classification is a substantive failure."""
        return classification in self.SUBSTANTIVE_CLASSIFICATIONS

    def get_retry_guidance(self, findings: List[str]) -> str:
        """Generate retry guidance based on findings."""
        if not findings:
            return "Retry with same goal, apply corrections from review."

        guidance = "Retry with same goal. Corrections needed:\n"
        for i, finding in enumerate(findings[:5], 1):  # Limit to top 5 findings
            guidance += f"  {i}. {finding}\n"
        return guidance


__all__ = [
    "FailureClassification",
    "RoutingDecision",
    "FailureEvidence",
    "EscalationResult",
    "GtxEscalationPolicy",
]
