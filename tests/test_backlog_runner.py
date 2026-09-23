"""Focused tests for the one-item GTX backlog queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from second_shift.backlog_manifest import BacklogStatus
from second_shift.backlog_runner import (
    BacklogRunner,
    DispatchOutcome,
    RunnerConfig,
)
from second_shift.state import StateFile


def _record(issue_id: str, status: str = "queued") -> dict:
    return {
        "issue_id": issue_id,
        "title": f"Title {issue_id}",
        "description": f"Description for {issue_id}",
        "acceptance_criteria": [f"Criterion for {issue_id}"],
        "context_size": 65536,
        "quality_importance": 0.8,
        "requires_benchmark_evidence": issue_id.endswith("2"),
        "status": status,
    }


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    path = tmp_path / "backlog.json"
    path.write_text(
        json.dumps({"items": [_record("TASK-001"), _record("TASK-002"), _record("TASK-003")]}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def runner(manifest_path: Path, tmp_path: Path) -> BacklogRunner:
    return BacklogRunner(RunnerConfig(manifest_path, str(tmp_path / "state")))


def test_selects_one_item_in_manifest_order(runner: BacklogRunner):
    items = runner.load_manifest()
    assert runner.select_next_item(items).issue_id == "TASK-001"


def test_skips_non_queued_items(runner: BacklogRunner):
    items = runner.load_manifest()
    items[0].status = BacklogStatus.COMPLETED
    assert runner.select_next_item(items).issue_id == "TASK-002"


def test_no_queued_item_is_idle(runner: BacklogRunner):
    items = runner.load_manifest()
    for item in items:
        item.status = BacklogStatus.COMPLETED
    assert runner.select_next_item(items) is None


def test_dispatch_passes_full_task_and_gtx_workflow(runner: BacklogRunner):
    captured = {}

    def dispatcher(**kwargs):
        captured.update(kwargs)
        return {"coding_dispatcher": {"selected_model": "p40_qwen35"}}

    items, outcome = runner.dispatch_next(dispatcher)
    assert isinstance(outcome, DispatchOutcome)
    assert outcome.item_id == "TASK-001"
    assert outcome.status is BacklogStatus.IN_PROGRESS
    assert captured["workflow"] == "gtx_direct_escalation"
    assert captured["goal_id"] == "TASK-001"
    assert captured["gtx_context_size"] == 65536
    assert captured["gtx_quality_importance"] == 0.8
    assert captured["requires_benchmark_evidence"] is False
    assert "Title TASK-001" in captured["task_description"]
    assert "Criterion for TASK-001" in captured["task_description"]
    assert next(item for item in items if item.issue_id == "TASK-001").status is BacklogStatus.IN_PROGRESS


def test_dispatch_does_not_mark_routing_as_completion(runner: BacklogRunner):
    items, _ = runner.dispatch_next(lambda **kwargs: {"selected_model": "p40_qwen35"})
    assert items[0].status is BacklogStatus.IN_PROGRESS
    assert runner.load_manifest()[0].status is BacklogStatus.IN_PROGRESS


def test_dispatch_idles_while_item_is_in_progress(runner: BacklogRunner):
    calls = []

    def dispatcher(**kwargs):
        calls.append(kwargs["goal_id"])
        return {"selected_model": "p40_qwen35"}

    items, first = runner.dispatch_next(dispatcher)
    repeated_items, repeated = runner.dispatch_next(dispatcher, items)

    assert first.item_id == "TASK-001"
    assert repeated is None
    assert [item.status for item in repeated_items] == [
        BacklogStatus.IN_PROGRESS,
        BacklogStatus.QUEUED,
        BacklogStatus.QUEUED,
    ]
    assert calls == ["TASK-001"]


def test_stale_snapshots_cannot_claim_same_item_twice(runner: BacklogRunner):
    stale_snapshot = runner.load_manifest()
    calls = []

    def dispatcher(**kwargs):
        calls.append(kwargs["goal_id"])
        return {"selected_model": "p40_qwen35"}

    first_items, first = runner.dispatch_next(dispatcher, stale_snapshot)
    second_items, second = runner.dispatch_next(dispatcher, stale_snapshot)

    assert first.item_id == "TASK-001"
    assert second is None
    assert calls == ["TASK-001"]
    assert first_items[0].status is BacklogStatus.IN_PROGRESS
    assert second_items[0].status is BacklogStatus.IN_PROGRESS
    assert runner.get_state()["claimed_count"] == 1


def test_only_selected_item_transitions(runner: BacklogRunner):
    items = runner.load_manifest()
    updated = runner.set_in_progress(items, items[0])
    assert [item.status for item in updated] == [
        BacklogStatus.IN_PROGRESS,
        BacklogStatus.QUEUED,
        BacklogStatus.QUEUED,
    ]


@pytest.mark.parametrize(
    "status",
    [BacklogStatus.COMPLETED, BacklogStatus.BLOCKED, BacklogStatus.ESCALATED],
)
def test_record_outcome_persists_lifecycle(runner: BacklogRunner, status):
    items, _ = runner.dispatch_next(lambda **kwargs: {"selected_model": "p40_qwen35"})
    updated = runner.record_outcome(items, "TASK-001", status, reason="test")
    assert updated[0].status is status
    assert runner.load_manifest()[0].status is status
    ledger = runner.state_file.get_ledger_entries()
    assert ledger[-1]["new_status"] == status.value


def test_restart_resumes_persisted_status_and_next_item(runner: BacklogRunner):
    items, _ = runner.dispatch_next(lambda **kwargs: {"selected_model": "p40_qwen35"})
    runner.record_outcome(items, "TASK-001", BacklogStatus.COMPLETED)
    restarted = BacklogRunner(runner.config)
    assert restarted.load_manifest()[0].status is BacklogStatus.COMPLETED
    assert restarted.select_next_item(restarted.load_manifest()).issue_id == "TASK-002"


def test_restart_idles_until_in_progress_item_has_outcome(runner: BacklogRunner):
    runner.dispatch_next(lambda **kwargs: {"selected_model": "p40_qwen35"})
    restarted = BacklogRunner(runner.config)

    items, outcome = restarted.dispatch_next(
        lambda **kwargs: {"selected_model": "should-not-run"}
    )

    assert outcome is None
    assert items[0].status is BacklogStatus.IN_PROGRESS
    assert items[1].status is BacklogStatus.QUEUED


def test_dispatch_exception_blocks_only_claimed_item(runner: BacklogRunner):
    def failing_dispatcher(**kwargs):
        raise RuntimeError("route failed")

    items, outcome = runner.dispatch_next(failing_dispatcher)
    assert outcome.status is BacklogStatus.BLOCKED
    assert items[0].status is BacklogStatus.BLOCKED
    assert items[1].status is BacklogStatus.QUEUED
    ledger = runner.state_file.get_ledger_entries()
    assert ledger[-2]["old_status"] == BacklogStatus.QUEUED.value
    assert ledger[-2]["new_status"] == BacklogStatus.IN_PROGRESS.value
    assert ledger[-1]["old_status"] == BacklogStatus.IN_PROGRESS.value
    assert ledger[-1]["new_status"] == BacklogStatus.BLOCKED.value


def test_dispatch_with_real_dispatcher_api(tmp_path: Path, manifest_path: Path):
    state = StateFile(str(tmp_path / "state"))
    from second_shift.dispatch import DefaultCodingDispatcher

    dispatcher = DefaultCodingDispatcher(state)
    runner = BacklogRunner(RunnerConfig(manifest_path, str(tmp_path / "state")))
    items, outcome = runner.dispatch_next(dispatcher.route_coding_request)
    assert outcome.routing["coding_dispatcher"]["selected_model"] == "p40_qwen35"
    assert items[0].status is BacklogStatus.IN_PROGRESS
