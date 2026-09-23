"""Deterministic role contracts for the repository coding workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


class WorkflowName(str, Enum):
    """Supported repository coding workflows."""

    LAYERED_REVIEW = "layered-review"
    DIRECT_ESCALATION = "direct-escalation"


@dataclass(frozen=True)
class WorkflowContract:
    """Roles and fallback boundaries that must remain stable."""

    name: WorkflowName
    primary_worker: str = "p40"
    routine_reviewer: str = "codex"
    final_verifier: str = "codex"
    copilot_repair_worker: str = "copilot"
    copilot_repair_fallback: str = "codex"
    second_shift_fallback: bool = False
    max_primary_attempts: int = 3


LAYERED_REVIEW = WorkflowContract(
    name=WorkflowName.LAYERED_REVIEW,
    second_shift_fallback=True,
)

DIRECT_ESCALATION = WorkflowContract(
    name=WorkflowName.DIRECT_ESCALATION,
    second_shift_fallback=False,
)

WORKFLOW_CONTRACTS = {
    WorkflowName.LAYERED_REVIEW.value: LAYERED_REVIEW,
    WorkflowName.DIRECT_ESCALATION.value: DIRECT_ESCALATION,
}

ROUTINE_REVIEWER = "codex"
FINAL_VERIFIER = "codex"
COPILOT_REPAIR_WORKER = "copilot"
COPILOT_REPAIR_FALLBACK = "codex"

# Operational/provider problems are recorded separately and never count as
# evidence that a worker failed to implement code.
NON_IMPLEMENTATION_PROVIDER_EVENTS = frozenset({
    "provider_unavailable",
    "quota_exhausted",
    "rate_limited",
    "timeout",
    "no_response",
    "authentication_failure",
    "provider_refusal",
})


def choose_repair_worker(*, copilot_available: bool) -> str:
    """Choose Copilot while usable, otherwise Codex as the repair worker."""

    return COPILOT_REPAIR_WORKER if copilot_available else COPILOT_REPAIR_FALLBACK


def get_workflow_contract(workflow: WorkflowName | str) -> WorkflowContract:
    """Resolve a workflow name while rejecting silent policy changes."""

    name = workflow.value if isinstance(workflow, WorkflowName) else workflow
    try:
        return WORKFLOW_CONTRACTS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown coding workflow: {workflow}") from exc


def provider_transition(*, from_worker: str, to_worker: str, reason: str) -> Dict[str, object]:
    """Build an explicit, non-failure Copilot-to-Codex transition record."""

    if from_worker != COPILOT_REPAIR_WORKER or to_worker != COPILOT_REPAIR_FALLBACK:
        raise ValueError("only Copilot-to-Codex repair failover is supported")
    if reason not in NON_IMPLEMENTATION_PROVIDER_EVENTS:
        raise ValueError("provider transition requires an operational reason")
    return {
        "event": "provider_transition",
        "from_worker": from_worker,
        "to_worker": to_worker,
        "reason": reason,
        "counts_as_implementation_failure": False,
    }
