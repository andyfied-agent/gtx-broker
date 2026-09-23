"""Second Shift model registry and failure ledger."""

from .state import StateFile
from .registry import ModelRegistry, ModelStatus, GoalClass
from .ledger import FailureLedger, FailureClassification
from .credit_state import ModelCreditState, CreditStatus
from .pool_recovery import check_pool_recovery, get_usable_member_ids, pool_recovery_status
from .dispatch import DefaultCodingDispatcher, ReviewOutcome, DispatcherAttempt
from .workflow_policy import (
    COPILOT_REPAIR_FALLBACK,
    COPILOT_REPAIR_WORKER,
    DIRECT_ESCALATION,
    FINAL_VERIFIER,
    LAYERED_REVIEW,
    ROUTINE_REVIEWER,
    WorkflowContract,
    WorkflowName,
    choose_repair_worker,
    get_workflow_contract,
    provider_transition,
)
from .backlog_manifest import (
    BacklogItem,
    BacklogStatus,
    P40_MAX_CONTEXT_SIZE,
    ManifestValidationError,
    load_manifest,
    validate_manifest,
    load_backlog_items,
    save_manifest,
)
from .backlog_runner import (
    BacklogRunner,
    DispatchOutcome,
    ItemLifecycle,
    RunnerConfig,
)
from .backlog_execution_adapter import (
    BacklogExecutionAdapter,
    ExecutionOutcome,
    ExecutionRequest,
    VerificationResult,
    WorkerResult,
)

__all__ = [
    "StateFile",
    "ModelRegistry",
    "ModelStatus",
    "GoalClass",
    "FailureLedger",
    "FailureClassification",
    "ModelCreditState",
    "CreditStatus",
    "check_pool_recovery",
    "get_usable_member_ids",
    "pool_recovery_status",
    "DefaultCodingDispatcher",
    "ReviewOutcome",
    "DispatcherAttempt",
    "WorkflowContract",
    "WorkflowName",
    "LAYERED_REVIEW",
    "DIRECT_ESCALATION",
    "ROUTINE_REVIEWER",
    "FINAL_VERIFIER",
    "COPILOT_REPAIR_WORKER",
    "COPILOT_REPAIR_FALLBACK",
    "choose_repair_worker",
    "get_workflow_contract",
    "provider_transition",
    # Backlog manifest
    "BacklogItem",
    "BacklogStatus",
    "P40_MAX_CONTEXT_SIZE",
    "ManifestValidationError",
    "load_manifest",
    "validate_manifest",
    "load_backlog_items",
    "save_manifest",
    "BacklogRunner",
    "DispatchOutcome",
    "ItemLifecycle",
    "RunnerConfig",
    "BacklogExecutionAdapter",
    "ExecutionOutcome",
    "ExecutionRequest",
    "VerificationResult",
    "WorkerResult",
]
