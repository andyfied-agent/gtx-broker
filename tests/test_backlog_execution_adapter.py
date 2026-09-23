from __future__ import annotations

import json
import subprocess
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from second_shift.backlog_execution_adapter import (
    BacklogExecutionAdapter,
    ExecutionRequest,
    VerificationResult,
    WorkerResult,
)
from second_shift.backlog_manifest import BacklogStatus


def manifest(path: Path) -> Path:
    path.write_text(json.dumps({"items": [
        {
            "issue_id": "TASK-001",
            "title": "First task",
            "description": "Implement the first task.",
            "acceptance_criteria": ["Tests pass"],
            "context_size": 65536,
            "quality_importance": 0.9,
            "requires_benchmark_evidence": False,
            "status": "queued",
        },
        {
            "issue_id": "TASK-002",
            "title": "Second task",
            "description": "Implement the second task.",
            "acceptance_criteria": ["Tests pass"],
            "context_size": 32768,
            "quality_importance": 0.5,
            "requires_benchmark_evidence": False,
            "status": "queued",
        },
    ]}))
    return path


def adapter(tmp_path, worker, test_runner=None, self_reviewer=None, reviewer=None, final_verifier=None, escalation_worker=None, max_attempts=3, router=None, workflow="gtx_direct_escalation", worktree_factory=None):
    return BacklogExecutionAdapter(
        manifest(tmp_path / "manifest.json"),
        tmp_path / "repo",
        state_dir=str(tmp_path / "state"),
        workflow=workflow,
        file_guidance="Use second_shift/ and tests/ only.",
        worker=worker,
        test_runner=test_runner or (lambda request: VerificationResult(True, ("tests passed",))),
        self_reviewer=self_reviewer,
        reviewer=reviewer or (lambda *args: VerificationResult(True, ("review passed",))),
        final_verifier=final_verifier or (lambda *args: VerificationResult(True, ("final passed",))),
        escalation_worker=escalation_worker,
        router=router,
        max_attempts=max_attempts,
        worktree_factory=worktree_factory or (lambda item: tmp_path / "worktrees" / item.issue_id),
    )


def test_default_workflow_and_full_context_are_persisted(tmp_path):
    requests = []

    def worker(request):
        requests.append(request)
        return WorkerResult(True, "success", changed_files=("second_shift/example.py",), evidence=("artifact committed",))

    runner = adapter(tmp_path, worker)
    outcome = runner.run_next()

    assert outcome.status == "completed"
    assert requests[0].issue_id == "TASK-001"
    assert requests[0].description == "Implement the first task."
    assert requests[0].acceptance_criteria == ("Tests pass",)
    assert requests[0].repository_path == str((tmp_path / "repo").resolve())
    assert requests[0].file_guidance == "Use second_shift/ and tests/ only."
    assert requests[0].workflow == "gtx_direct_escalation"
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["status"] == "completed"
    attempts = runner.dispatcher.get_attempt_history("TASK-001")
    assert [entry["model_id"] for entry in attempts] == ["p40_qwen35", "codex"]
    assert attempts[0]["classification"] == "success"
    assert attempts[1]["is_final_verification"] is True


def test_default_executor_registry_supports_gtx_p40_alias(tmp_path):
    runner = BacklogExecutionAdapter(
        manifest(tmp_path / "manifest.json"), tmp_path / "repo",
        state_dir=str(tmp_path / "state"),
        worktree_factory=lambda item: tmp_path / "worktrees" / item.issue_id,
    )

    assert "p40_qwen35" in runner.executors
    assert "codex" in runner.executors


def test_p40_self_review_precedes_codex_review_and_is_not_the_gate(tmp_path):
    events = []

    def worker(request):
        events.append("worker")
        return WorkerResult(True, changed_files=("implementation.py",))

    def tests(request):
        events.append("tests")
        return VerificationResult(True, ("tests passed",))

    def self_review(request, worker_result, test_result):
        events.append("self_review")
        return VerificationResult(False, ("P40 found a possible risk",), "rejected_code")

    def review(request, worker_result, test_result):
        events.append("codex_review")
        assert "P40 found a possible risk" in worker_result.evidence
        return VerificationResult(True, ("Codex approved",))

    def final(request, worker_result, review_result):
        events.append("final_verification")
        return VerificationResult(True, ("final verification passed",))

    runner = adapter(
        tmp_path, worker, test_runner=tests, self_reviewer=self_review,
        reviewer=review, final_verifier=final,
    )

    outcome = runner.run_next()

    assert outcome.status == "completed"
    assert events == ["worker", "tests", "self_review", "codex_review", "final_verification"]


def test_p40_self_review_mutation_rejects_stale_test_candidate(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
    target = worktree / "implementation.py"
    target.write_text("baseline\n")
    subprocess.run(["git", "-C", str(worktree), "add", "implementation.py"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-m", "baseline"], check=True, capture_output=True)
    codex_calls = []

    def mutating_self_review(request, worker_result, tests):
        target.write_text("changed during self-review\n")
        return VerificationResult(True, ("self-review claimed success",))

    runner = adapter(
        tmp_path,
        lambda request: WorkerResult(True, changed_files=("implementation.py",)),
        self_reviewer=mutating_self_review,
        reviewer=lambda *args: codex_calls.append("review") or VerificationResult(True),
        final_verifier=lambda *args: codex_calls.append("final") or VerificationResult(True),
        worktree_factory=lambda item: worktree,
    )

    outcome = runner.run_next()

    assert outcome.status == "queued"
    assert codex_calls == []
    assert "modified the worktree" in " ".join(outcome.evidence)
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["classification"] == "tests_failed"



def test_workflow_can_be_overridden_explicitly(tmp_path):
    requests = []
    runner = BacklogExecutionAdapter(
        manifest(tmp_path / "manifest.json"),
        tmp_path / "repo",
        state_dir=str(tmp_path / "state"),
        workflow="layered-review",
        worker=lambda request: requests.append(request) or WorkerResult(True, changed_files=("implementation.py",)),
        test_runner=lambda request: VerificationResult(True),
        reviewer=lambda *args: VerificationResult(True),
        final_verifier=lambda *args: VerificationResult(True),
        router=lambda item, workflow: {"coding_dispatcher": {"selected_model": "p40", "workflow": workflow}},
        worktree_factory=lambda item: tmp_path / "worktrees" / item.issue_id,
    )

    runner.run_next()

    assert requests[0].workflow == "layered-review"


def test_items_are_processed_in_order_and_one_at_a_time(tmp_path):
    seen = []

    def worker(request):
        seen.append(request.issue_id)
        return WorkerResult(True, changed_files=("implementation.py",))

    runner = adapter(tmp_path, worker)
    assert runner.run_next().issue_id == "TASK-001"
    assert runner.run_next().issue_id == "TASK-002"
    assert runner.run_next() is None
    assert seen == ["TASK-001", "TASK-002"]


def test_final_verification_is_required_for_completion(tmp_path):
    runner = adapter(
        tmp_path,
        lambda request: WorkerResult(True, changed_files=("implementation.py",)),
        final_verifier=lambda *args: VerificationResult(False, ("verification failed",)),
    )

    outcome = runner.run_next()

    assert outcome.status == "queued"
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["status"] == "queued"
    assert runner.dispatcher.get_p40_failure_count("TASK-001", "coding") == 0


def test_final_verification_outage_preserves_candidate(tmp_path):
    calls = []
    verification_calls = []

    def final_verifier(*args):
        verification_calls.append(True)
        if len(verification_calls) == 1:
            return VerificationResult(False, ("Codex final verification timed out",), "review_unavailable")
        return VerificationResult(True, ("Codex final verification passed",))

    runner = adapter(
        tmp_path,
        lambda request: calls.append(request.issue_id) or WorkerResult(True, changed_files=("implementation.py",)),
        final_verifier=final_verifier,
    )
    manifest_data = json.loads(runner.manifest_path.read_text())
    runner.manifest_path.write_text(json.dumps({"items": manifest_data["items"][:1]}))

    outcome = runner.run_next()

    assert outcome.status == "blocked"
    assert calls == ["TASK-001"]
    resumed = runner.run_next()
    assert resumed.status == "completed"
    assert calls == ["TASK-001"]
    assert len(verification_calls) == 2
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["phase"] == "completed"
    assert runner.dispatcher.get_p40_failure_count("TASK-001", "coding") == 0


def test_plan_only_success_cannot_complete(tmp_path):
    runner = adapter(tmp_path, lambda request: WorkerResult(True, "success"))

    outcome = runner.run_next()

    assert outcome.status == "queued"
    assert outcome.worker == "p40_qwen35"
    assert "without changed-file/artifact evidence" in " ".join(outcome.evidence)


def test_default_codex_gates_fail_closed(tmp_path):
    runner = BacklogExecutionAdapter(
        manifest(tmp_path / "manifest.json"), tmp_path / "repo",
        state_dir=str(tmp_path / "state"),
        worker=lambda request: WorkerResult(True, changed_files=("implementation.py",)),
        test_runner=lambda request: VerificationResult(True),
        worktree_factory=lambda item: tmp_path / "worktrees" / item.issue_id,
        workflow="layered-review",
        router=lambda item, workflow: {"coding_dispatcher": {"selected_model": "p40"}},
    )

    outcome = runner.run_next()

    assert outcome.status == "blocked"
    assert "CODEX_REVIEW_COMMAND is not configured" in " ".join(outcome.evidence)


def test_codex_review_infrastructure_failure_is_not_p40_code_failure(tmp_path):
    calls = []
    review_calls = []

    def reviewer(*args):
        review_calls.append(True)
        if len(review_calls) == 1:
            return VerificationResult(False, ("Codex review timed out",), "review_unavailable")
        return VerificationResult(True, ("Codex review passed",))

    runner = adapter(
        tmp_path,
        lambda request: calls.append(request.issue_id) or WorkerResult(True, changed_files=("implementation.py",)),
        reviewer=reviewer,
    )
    manifest_data = json.loads(runner.manifest_path.read_text())
    runner.manifest_path.write_text(json.dumps({"items": manifest_data["items"][:1]}))

    outcome = runner.run_next()

    assert outcome.status == "blocked"
    assert calls == ["TASK-001"]
    resumed = runner.run_next()
    assert resumed.status == "completed"
    assert calls == ["TASK-001"]
    assert len(review_calls) == 2
    attempts = runner.dispatcher.get_attempt_history("TASK-001")
    assert attempts[0]["classification"] == "success"
    assert attempts[1]["is_final_verification"] is True
    assert runner.dispatcher.get_p40_failure_count("TASK-001", "coding") == 0
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["phase"] == "completed"


def test_review_resume_persistence_failure_keeps_gate_resumable(tmp_path):
    review_calls = []
    runner = adapter(
        tmp_path,
        lambda request: WorkerResult(True, changed_files=("implementation.py",)),
        reviewer=lambda *args: review_calls.append(True) or (
            VerificationResult(False, ("temporary outage",), "review_unavailable")
            if len(review_calls) == 1 else VerificationResult(True, ("review passed",))
        ),
    )
    assert runner.run_next().status == "blocked"

    original = runner._record_candidate
    runner._record_candidate = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable"))
    failed = runner.run_next()
    assert failed.status == "blocked"
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["phase"] == "review_pending"
    assert runner.runner.load_manifest()[1].status == BacklogStatus.QUEUED

    runner._record_candidate = original
    assert runner.run_next().status == "completed"
    assert len(review_calls) == 3


def test_final_resume_persistence_failure_keeps_gate_resumable(tmp_path):
    final_calls = []

    def final_verifier(*args):
        final_calls.append(True)
        if len(final_calls) == 1:
            return VerificationResult(False, ("temporary outage",), "review_unavailable")
        return VerificationResult(True, ("final verification passed",))

    runner = adapter(
        tmp_path,
        lambda request: WorkerResult(True, changed_files=("implementation.py",)),
        final_verifier=final_verifier,
    )
    assert runner.run_next().status == "blocked"

    original = runner.dispatcher.record_final_verification
    runner.dispatcher.record_final_verification = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable"))
    failed = runner.run_next()
    assert failed.status == "blocked"
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["phase"] == "final_verification_pending"
    assert runner.runner.load_manifest()[1].status == BacklogStatus.QUEUED

    runner.dispatcher.record_final_verification = original
    assert runner.run_next().status == "completed"
    assert len(final_calls) == 3


def test_codex_repair_uses_exec_cd_and_prompt_stdin(tmp_path, monkeypatch):
    runner = adapter(tmp_path, lambda request: WorkerResult(True, changed_files=("unused.py",)))
    request = ExecutionRequest(
        "TASK-001", "Repair", "Repair it", ("Tests pass",), str(tmp_path),
        str(tmp_path / "worktree"), 65536, 0.8, False, selected_model="codex",
    )
    runner._git_snapshot = Mock(side_effect=[("head", "before", ("repair.py",)), ("head", "after", ("repair.py",))])
    monkeypatch.setenv("CODEX_COMMAND", "codex")
    completed = subprocess.CompletedProcess([], 0, stdout="repair complete", stderr="")
    with patch("second_shift.backlog_execution_adapter.subprocess.run", return_value=completed) as run:
        result = runner._run_codex_worker(request)

    argv = run.call_args.args[0]
    assert argv[:4] == ["codex", "exec", "--cd", str(tmp_path / "worktree")]
    assert "--in" not in argv and "-z" not in argv
    assert run.call_args.kwargs["input"]
    assert json.loads(run.call_args.kwargs["input"])["role"] == "repair_executor"
    assert result.success is True


def test_codex_gate_uses_exec_cd_stdin_and_output_schema(tmp_path, monkeypatch):
    runner = adapter(tmp_path, lambda request: WorkerResult(True, changed_files=("unused.py",)))
    request = ExecutionRequest(
        "TASK-001", "Review", "Review it", ("Tests pass",), str(tmp_path),
        str(tmp_path / "worktree"), 65536, 0.8, False,
    )
    monkeypatch.setenv("CODEX_COMMAND", "codex")
    completed = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"passed": True, "findings": [], "evidence": ["clean"]}), stderr="",
    )
    with patch("second_shift.backlog_execution_adapter.subprocess.run", return_value=completed) as run:
        result = runner._run_codex_gate(
            "review", request, WorkerResult(True, changed_files=("x.py",)),
            VerificationResult(True),
        )

    argv = run.call_args.args[0]
    assert argv[:4] == ["codex", "exec", "--cd", str(tmp_path / "worktree")]
    assert "--output-schema" in argv
    assert "--in" not in argv and "-z" not in argv
    assert json.loads(run.call_args.kwargs["input"])["gate"] == "review"
    assert result.passed is True
    assert result.failure_kind == "approved"


def test_explicit_codex_route_uses_escalation_worker(tmp_path):
    calls = []

    def p40(request):
        calls.append("p40")
        raise AssertionError("P40 must not override explicit Codex routing")

    def codex(request):
        calls.append(request.selected_model)
        return WorkerResult(True, changed_files=("repair.py",))

    runner = adapter(
        tmp_path, p40, escalation_worker=codex,
        workflow="layered-review",
        router=lambda item, workflow: {"coding_dispatcher": {"selected_model": "codex"}},
    )

    outcome = runner.run_next()

    assert outcome.status == "completed"
    assert outcome.worker == "codex"
    assert calls == ["codex"]


def test_unconfigured_selected_worker_fails_closed(tmp_path):
    calls = []
    runner = adapter(
        tmp_path,
        lambda request: calls.append(request.selected_model) or WorkerResult(True, changed_files=("wrong.py",)),
        workflow="layered-review",
        router=lambda item, workflow: {"coding_dispatcher": {"selected_model": "second_shift"}},
    )

    outcome = runner.run_next()

    assert outcome.status == "blocked"
    assert "no executor configured" in outcome.evidence[0]
    assert calls == []


def test_codex_gate_requires_structured_boolean_result():
    assert BacklogExecutionAdapter._parse_codex_result('{"passed": false, "findings": ["missing test"]}', "review") == VerificationResult(False, ("missing test",))
    assert not BacklogExecutionAdapter._parse_codex_result("approved", "review").passed


def test_git_snapshot_changes_when_already_dirty_file_is_edited(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    target = tmp_path / "implementation.py"
    target.write_text("baseline\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "implementation.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "baseline"], check=True, capture_output=True)
    target.write_text("dirty first edit\n")
    _, first_fingerprint, first_files = BacklogExecutionAdapter._git_snapshot(str(tmp_path))
    target.write_text("dirty second edit\n")
    _, second_fingerprint, second_files = BacklogExecutionAdapter._git_snapshot(str(tmp_path))

    assert first_files == second_files == ("implementation.py",)
    assert first_fingerprint != second_fingerprint


def test_restart_inspects_existing_in_progress_without_duplicate_dispatch(tmp_path):
    requests = []
    runner = adapter(tmp_path, lambda request: requests.append(request) or WorkerResult(True))
    persisted_items = runner._items()
    persisted_items[0].status = BacklogStatus.IN_PROGRESS

    def leave_in_progress(registry, ledger):
        registry["backlog_items"] = [item.to_dict() for item in persisted_items]
        registry["backlog_execution"] = {"items": {"TASK-001": {"status": "in_progress", "attempts": 1, "selected_model": "p40"}}}
        return registry, ledger

    runner.state.update_state(leave_in_progress)
    outcome = runner.run_next()

    assert outcome.issue_id == "TASK-001"
    assert outcome.status == "blocked"
    assert requests == []
    assert "interrupted execution" in outcome.evidence[0]


def test_timeout_retries_without_substantive_failure(tmp_path):
    runner = adapter(tmp_path, lambda request: WorkerResult(False, "timeout", evidence=("worker timeout",)))

    first = runner.run_next()
    second = runner.run_next()

    assert first.status == "queued"
    assert second.status == "queued"
    entries = runner.state.get_ledger_entries()
    assert all("substantive" not in entry.get("event", "") for entry in entries)


def test_timeout_is_recorded_and_controller_selects_next_executor(tmp_path):
    workers = []

    def p40(request):
        workers.append(request.selected_model)
        return WorkerResult(False, "timeout", evidence=("P40 timed out",))

    def codex(request):
        workers.append(request.selected_model)
        return WorkerResult(True, "success", changed_files=("repair.py",), evidence=("Codex repaired artifact",))

    runner = adapter(tmp_path, p40, escalation_worker=codex, max_attempts=2)
    assert runner.run_next().status == "queued"
    outcome = runner.run_next()

    assert outcome.status == "queued"
    assert workers == ["p40_qwen35", "p40_qwen35"]
    attempts = runner.dispatcher.get_attempt_history("TASK-001")
    assert len(attempts) == 2
    assert all(entry["classification"] == "timeout" for entry in attempts)


def test_repeated_non_substantive_failures_reach_codex_via_controller(tmp_path):
    workers = []

    def p40(request):
        workers.append(request.selected_model)
        return WorkerResult(False, "timeout", evidence=("P40 timed out",))

    def codex(request):
        workers.append(request.selected_model)
        return WorkerResult(True, "success", changed_files=("repair.py",))

    runner = adapter(tmp_path, p40, escalation_worker=codex)
    assert runner.run_next().status == "queued"
    assert runner.run_next().status == "queued"
    assert runner.run_next().status == "queued"
    outcome = runner.run_next()

    assert outcome.status == "completed"
    assert workers == ["p40_qwen35", "p40_qwen35", "p40_qwen35", "codex"]


def test_worker_exception_is_recorded_and_releases_queue(tmp_path):
    def broken_worker(request):
        raise FileNotFoundError("hermes-compute01")

    runner = adapter(tmp_path, broken_worker)
    outcome = runner.run_next()

    assert outcome.status == "queued"
    assert "FileNotFoundError" in " ".join(outcome.evidence)
    assert runner.runner.load_manifest()[0].status.value == "queued"
    assert runner.state.get_registry()["backlog_execution"]["items"]["TASK-001"]["phase"] == "exception"
