"""Contract tests for the broker/workstation interface seams."""

import json
from pathlib import Path

import pytest

from second_shift import (
    BacklogExecutionAdapter,
    BacklogItem,
    BacklogStatus,
    DefaultCodingDispatcher,
    FailureLedger,
    StateFile,
    WorkflowName,
    WorkerResult,
    VerificationResult,
    get_workflow_contract,
    save_manifest,
)


def make_item(issue_id="TASK-001"):
    return BacklogItem(
        issue_id=issue_id,
        title="Implement the real task",
        description="The worker must receive this full description, not just the ID.",
        acceptance_criteria=["The implementation is tested"],
        context_size=65536,
        quality_importance=0.8,
        requires_benchmark_evidence=True,
        status=BacklogStatus.QUEUED,
    )


def test_state_transaction_preserves_namespaces_and_failure_schema(tmp_path):
    state = StateFile(str(tmp_path / "state"))
    state.save_registry({"models": [], "gtx_broker": {"current_state": "x"}})

    def mutate(registry, entries):
        registry["coding_dispatcher"] = {"selected_model": "p40"}
        return registry, entries

    state.update_state(mutate)
    registry = state.get_registry()
    assert registry["models"] == []
    assert registry["gtx_broker"] == {"current_state": "x"}
    assert registry["coding_dispatcher"]["selected_model"] == "p40"

    ledger = FailureLedger(state)
    ledger.record_failure(
        model_id="p40", provider="p40", goal_id="TASK-001", goal_class="coding",
        classification="timeout", evidence=["worker timeout"],
        credit_status="available", registry=registry,
    )
    entry = state.get_ledger_entries()[-1]
    assert entry["classification"] == "timeout"
    assert entry["goal_id"] == "TASK-001"
    assert "ledger_type" not in entry


def test_workflow_contracts_define_roles_and_attempt_bound(tmp_path):
    del tmp_path
    for name, fallback in ((WorkflowName.LAYERED_REVIEW, True),
                           (WorkflowName.DIRECT_ESCALATION, False)):
        contract = get_workflow_contract(name)
        assert contract.primary_worker == "p40"
        assert contract.routine_reviewer == "codex"
        assert contract.final_verifier == "codex"
        assert contract.max_primary_attempts == 3
        assert contract.second_shift_fallback is fallback

    with pytest.raises(ValueError):
        get_workflow_contract("not-a-workflow")


def test_dispatcher_persists_selected_model_and_workflow(tmp_path):
    dispatcher = DefaultCodingDispatcher(StateFile(str(tmp_path / "state")))
    registry = {"models": [{"id": "p40", "status": "active"}]}
    result = dispatcher.route_coding_request(
        goal_id="TASK-001", goal_class="coding", registry=registry,
        workflow=WorkflowName.DIRECT_ESCALATION,
        task_description="Implement the actual requested feature",
    )
    assert result["coding_dispatcher"]["selected_model"] == "p40"
    assert result["coding_dispatcher"]["workflow"] == "direct-escalation"


def test_adapter_registers_normal_p40_alias_and_codex_executor(tmp_path):
    manifest = tmp_path / "manifest.json"
    save_manifest(manifest, [make_item()])
    adapter = BacklogExecutionAdapter(
        manifest, tmp_path, state_dir=str(tmp_path / "state"),
    )
    assert "p40" in adapter.executors
    assert "p40_qwen35" in adapter.executors
    assert "codex" in adapter.executors


def test_adapter_propagates_real_item_to_selected_worker(tmp_path):
    manifest = tmp_path / "manifest.json"
    item = make_item()
    save_manifest(manifest, [item])
    captured = []

    def worker(request):
        captured.append(request)
        return WorkerResult(True, "success", "implemented", ("src/feature.py",))

    adapter = BacklogExecutionAdapter(
        manifest, tmp_path, state_dir=str(tmp_path / "state"), worker=worker,
        worktree_factory=lambda _item: tmp_path,
        test_runner=lambda _request: VerificationResult(True, ("tests passed",), "approved"),
        reviewer=lambda _request, _worker, _tests: VerificationResult(True, ("review passed",), "approved"),
        final_verifier=lambda _request, _worker, _review: VerificationResult(True, ("verified",), "approved"),
    )

    outcome = adapter.run_next()
    assert outcome is not None
    assert outcome.status == "completed"
    assert len(captured) == 1
    request = captured[0]
    assert request.issue_id == item.issue_id
    assert request.title == item.title
    assert request.description == item.description
    assert request.acceptance_criteria == tuple(item.acceptance_criteria)
    assert request.context_size == item.context_size
    assert request.quality_importance == item.quality_importance
    assert request.requires_benchmark_evidence is True
    assert request.selected_model in {"p40", "p40_qwen35"}


def test_failure_classification_boundary_is_explicit():
    dispatcher = DefaultCodingDispatcher(StateFile())
    assert dispatcher.classify_as_substantive("tests_failed")
    assert dispatcher.classify_as_substantive("incorrect_code")
    assert not dispatcher.classify_as_substantive("timeout")
    assert not dispatcher.classify_as_substantive("review_unavailable")
