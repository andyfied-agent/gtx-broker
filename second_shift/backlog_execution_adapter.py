"""Durable execution adapter built on the atomic backlog runner."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .backlog_manifest import BacklogItem, BacklogStatus
from .backlog_runner import BacklogRunner, RunnerConfig
from .dispatch import DefaultCodingDispatcher
from .state import StateFile
from .workflow_policy import WorkflowName

NON_SUBSTANTIVE = frozenset({
    "no_code", "timeout", "endpoint_unavailable", "authentication",
    "credit_exhausted", "rate_limited", "policy_refusal", "context_limit",
    "review_unavailable", "unknown",
})
SUBSTANTIVE = frozenset({"partial_code", "incorrect_code", "tests_failed"})


@dataclass(frozen=True)
class ExecutionRequest:
    issue_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    repository_path: str
    worktree_path: str
    context_size: int
    quality_importance: float
    requires_benchmark_evidence: bool
    file_guidance: str = ""
    workflow: str = "gtx_direct_escalation"
    selected_model: str = "p40"


@dataclass(frozen=True)
class WorkerResult:
    success: bool
    classification: str = "success"
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    test_output: str = ""
    test_passed: bool = False
    exit_code: int = 0
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    evidence: tuple[str, ...] = ()
    failure_kind: str = "rejected_code"


@dataclass(frozen=True)
class ExecutionOutcome:
    issue_id: str
    status: str
    attempt_number: int
    worker: str
    evidence: tuple[str, ...] = ()


Worker = Callable[[ExecutionRequest], WorkerResult]
TestRunner = Callable[[ExecutionRequest], VerificationResult]
Reviewer = Callable[[ExecutionRequest, WorkerResult, VerificationResult], VerificationResult]
SelfReviewer = Reviewer
Router = Callable[[BacklogItem, str], Mapping[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(value: WorkerResult | Mapping[str, Any]) -> WorkerResult:
    if isinstance(value, WorkerResult):
        return value
    return WorkerResult(
        bool(value.get("success")), str(value.get("classification", "unknown")),
        str(value.get("summary", "")), tuple(value.get("changed_files", ())),
        str(value.get("test_output", "")), bool(value.get("test_passed", False)),
        int(value.get("exit_code", 0)), tuple(value.get("evidence", ())),
    )


def _verification(value: VerificationResult | Mapping[str, Any]) -> VerificationResult:
    if isinstance(value, VerificationResult):
        return value
    return VerificationResult(
        bool(value.get("passed")), tuple(value.get("evidence", ())),
        str(value.get("failure_kind", "rejected_code")),
    )


class BacklogExecutionAdapter:
    """Execute one item through the durable dispatcher and backlog runner.

    The dispatcher/controller is the sole retry and escalation authority.
    ``BacklogRunner`` owns the atomic queue lifecycle; this adapter only stores
    execution checkpoints and evidence around those authoritative transitions.
    """

    STATE_KEY = "backlog_execution"
    CODEX_GATE_SCHEMA = {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["passed", "findings", "evidence"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        manifest_path: Path,
        repository_path: Path,
        *,
        state_dir: str | None = None,
        workflow: str | WorkflowName = "gtx_direct_escalation",
        file_guidance: str = "",
        worker: Worker | None = None,
        executors: Mapping[str, Worker] | None = None,
        test_runner: TestRunner | None = None,
        self_reviewer: SelfReviewer | None = None,
        reviewer: Reviewer | None = None,
        final_verifier: Reviewer | None = None,
        escalation_worker: Worker | None = None,
        router: Router | None = None,
        worktree_factory: Callable[[BacklogItem], Path] | None = None,
        max_attempts: int = 3,
        worker_timeout_seconds: float | None = None,
        codex_timeout_seconds: float | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.repository_path = Path(repository_path).resolve()
        self.state = StateFile(state_dir)
        self.runner = BacklogRunner(RunnerConfig(self.manifest_path, state_dir))
        self.dispatcher = DefaultCodingDispatcher(self.state)
        self.workflow = workflow.value if isinstance(workflow, WorkflowName) else str(workflow)
        self.file_guidance = file_guidance
        self.router = router
        self.test_runner = test_runner or self._run_repository_tests
        self.self_reviewer = self_reviewer
        self.reviewer = reviewer or self._run_codex_review
        self.final_verifier = final_verifier or self._run_codex_final_verification
        self.worktree_factory = worktree_factory or self._create_worktree
        self.worker_timeout_seconds = worker_timeout_seconds or float(
            os.environ.get("P40_WORKER_TIMEOUT_SECONDS", "1800")
        )
        self.codex_timeout_seconds = codex_timeout_seconds or float(
            os.environ.get("CODEX_REVIEW_TIMEOUT_SECONDS", "900")
        )
        # max_attempts remains accepted for compatibility with callers of the
        # first adapter, but retry/escalation decisions now come only from the
        # dispatcher/controller.
        self.max_attempts = max_attempts
        self.executors: dict[str, Worker] = dict(executors or {})
        if worker is not None:
            self.executors.setdefault("p40", worker)
            self.executors.setdefault("p40_qwen35", worker)
            if self.self_reviewer is None:
                self.self_reviewer = self._injected_worker_self_review
        else:
            self.executors.setdefault("p40", self._run_p40_worker)
            self.executors.setdefault("p40_qwen35", self._run_p40_worker)
            if self.self_reviewer is None:
                self.self_reviewer = self._run_p40_self_review
        if escalation_worker is not None:
            self.executors.setdefault("codex", escalation_worker)
        else:
            self.executors.setdefault("codex", self._run_codex_worker)

    def _items(self) -> list[BacklogItem]:
        return self.runner.load_manifest()

    def _create_worktree(self, item: BacklogItem) -> Path:
        worktrees = self.repository_path / ".worktrees"
        worktree = worktrees / item.issue_id.replace("/", "-")
        if not worktree.exists():
            worktrees.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "-C", str(self.repository_path), "worktree", "add", "--detach", str(worktree), "HEAD"],
                check=True, capture_output=True, text=True,
            )
        return worktree

    def _dispatch_route(self, **kwargs: Any) -> Mapping[str, Any]:
        item = next(item for item in self._items() if item.issue_id == kwargs["goal_id"])
        if self.router is not None:
            return self.router(item, str(kwargs.get("workflow", self.workflow)))
        return self.dispatcher.route_coding_request(**kwargs)

    def _request(self, item: BacklogItem, routing: Mapping[str, Any]) -> ExecutionRequest:
        selected_model = str(routing.get("coding_dispatcher", {}).get("selected_model", "p40"))
        return ExecutionRequest(
            item.issue_id, item.title, item.description, tuple(item.acceptance_criteria),
            str(self.repository_path), str(self.worktree_factory(item)), item.context_size,
            item.quality_importance, item.requires_benchmark_evidence, self.file_guidance,
            self.workflow, selected_model,
        )

    @staticmethod
    def _git_snapshot(worktree: str) -> tuple[str, str, tuple[str, ...]]:
        """Return HEAD, a content fingerprint, and changed paths.

        The fingerprint includes the actual diff and untracked-file bytes, so
        editing an already-dirty file is still recognized as new work.
        """
        try:
            head = subprocess.run(
                ["git", "-C", worktree, "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            diff = subprocess.run(
                ["git", "-C", worktree, "diff", "--binary", "HEAD"],
                check=True, capture_output=True,
            ).stdout
            status = subprocess.run(
                ["git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"],
                check=True, capture_output=True, text=True,
            ).stdout
            untracked = subprocess.run(
                ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            digest = hashlib.sha256(head.encode() + b"\0" + diff + b"\0").copy()
            for relative in sorted(untracked):
                path = Path(worktree) / relative
                if path.is_file():
                    digest.update(relative.encode() + b"\0" + path.read_bytes())
            files = tuple(line[3:] if len(line) > 3 else line for line in status.splitlines() if line.strip())
            return head, digest.hexdigest(), files
        except (OSError, subprocess.CalledProcessError):
            return "", "", ()

    def _run_p40_worker(self, request: ExecutionRequest) -> WorkerResult:
        command = os.environ.get("P40_HERMES_COMMAND") or shutil.which("hermes-compute01")
        if not command:
            return WorkerResult(False, "endpoint_unavailable", evidence=("P40_HERMES_COMMAND is not configured",))
        before_head, before_fingerprint, before_files = self._git_snapshot(request.worktree_path)
        prompt = json.dumps({
            **asdict(request), "require_implementation_artifact": True,
            "instruction": "Implement the task. A plan-only response is not success; leave changed files or a committed implementation and report them.",
        }, indent=2, sort_keys=True)
        env = os.environ.copy()
        if env.get("P40_HERMES_HOME"):
            env["HERMES_HOME"] = env["P40_HERMES_HOME"]
        try:
            completed = subprocess.run(
                [command, "--in", request.worktree_path, "--yolo", "-z", prompt],
                cwd=request.worktree_path, env=env, capture_output=True, text=True,
                timeout=self.worker_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return WorkerResult(False, "timeout", summary=str(exc), evidence=("P40 worker timed out",))
        after_head, after_fingerprint, after_files = self._git_snapshot(request.worktree_path)
        changed = tuple(sorted(set(before_files) | set(after_files)))
        if before_head != after_head or before_fingerprint != after_fingerprint:
            if not changed:
                changed = ("<worktree-diff>",)
        if completed.returncode != 0:
            return WorkerResult(False, "unknown", completed.stdout[-4000:], changed, exit_code=completed.returncode, evidence=(completed.stderr[-1000:] or "P40 worker failed",))
        if before_head == after_head and before_fingerprint == after_fingerprint:
            return WorkerResult(False, "no_code", completed.stdout[-4000:], evidence=("P40 returned successfully without an implementation artifact or changed files",))
        return WorkerResult(True, "success", completed.stdout[-4000:], changed, evidence=(f"P40 changed {len(changed)} file(s)",))

    @staticmethod
    def _injected_worker_self_review(*args) -> VerificationResult:
        return VerificationResult(
            True, ("P40 self-review skipped for an injected test executor",), "approved"
        )

    def _run_p40_self_review(
        self, request: ExecutionRequest, worker_result: WorkerResult,
        tests: VerificationResult,
    ) -> VerificationResult:
        """Run P40's preliminary self-review before independent Codex review."""
        command = os.environ.get("P40_SELF_REVIEW_COMMAND") or os.environ.get("P40_HERMES_COMMAND") or shutil.which("hermes-compute01")
        if not command:
            return VerificationResult(
                False, ("P40 self-review command is not configured",), "review_unavailable"
            )
        before_head, before_fingerprint, before_files = self._git_snapshot(request.worktree_path)
        prompt = json.dumps({
            "role": "p40_self_review",
            "request": asdict(request),
            "worker": asdict(worker_result),
            "tests": asdict(tests),
            "instruction": (
                "Review your implementation in the current worktree without editing it. "
                "Check the task, acceptance criteria, changed files, and test evidence. "
                "Report concrete findings and any remaining risks for an independent Codex reviewer."
            ),
        }, indent=2, sort_keys=True)
        completed = None
        failure: VerificationResult | None = None
        try:
            completed = subprocess.run(
                [command, "--in", request.worktree_path, "--yolo", "-z", prompt],
                cwd=request.worktree_path, capture_output=True, text=True,
                timeout=self.worker_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            failure = VerificationResult(False, ("P40 self-review timed out",), "review_unavailable")
        except OSError as exc:
            failure = VerificationResult(False, (f"P40 self-review could not start: {exc}",), "review_unavailable")
        after_head, after_fingerprint, after_files = self._git_snapshot(request.worktree_path)
        if before_head != after_head or before_fingerprint != after_fingerprint:
            return VerificationResult(
                False,
                (
                    "P40 self-review modified the worktree; review was rejected",
                    f"before files={before_files}", f"after files={after_files}",
                ),
                "self_review_mutated",
            )
        if failure is not None:
            return failure
        assert completed is not None
        output = (completed.stdout or completed.stderr or "").strip()[-4000:]
        if completed.returncode != 0:
            return VerificationResult(
                False, ("P40 self-review process failed", output), "review_unavailable"
            )
        return VerificationResult(
            True, ("P40 self-review completed", output), "approved"
        )

    def _run_codex_worker(self, request: ExecutionRequest) -> WorkerResult:
        """Run the default Codex repair executor after broker escalation."""
        command = os.environ.get("CODEX_REPAIR_COMMAND") or os.environ.get("CODEX_COMMAND")
        if not command:
            return WorkerResult(False, "endpoint_unavailable", evidence=("CODEX_COMMAND is not configured",))
        before_head, before_fingerprint, before_files = self._git_snapshot(request.worktree_path)
        prompt = json.dumps({
            **asdict(request),
            "role": "repair_executor",
            "require_implementation_artifact": True,
            "instruction": "Repair or implement the task in the worktree. A plan-only response is not success; leave changed files or a committed implementation.",
        }, indent=2, sort_keys=True)
        try:
            completed = subprocess.run(
                self._codex_exec_argv(command, request.worktree_path),
                cwd=request.worktree_path, capture_output=True, text=True,
                input=prompt,
                timeout=self.codex_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return WorkerResult(False, "timeout", summary=str(exc), evidence=("Codex repair worker timed out",))
        except OSError as exc:
            return WorkerResult(False, "endpoint_unavailable", summary=str(exc), evidence=("Codex repair command could not be started",))
        after_head, after_fingerprint, after_files = self._git_snapshot(request.worktree_path)
        changed = tuple(sorted(set(before_files) | set(after_files)))
        changed_artifact = before_head != after_head or before_fingerprint != after_fingerprint
        if changed_artifact and not changed:
            changed = ("<worktree-diff>",)
        if completed.returncode != 0:
            return WorkerResult(False, "unknown", completed.stdout[-4000:], changed, exit_code=completed.returncode, evidence=(completed.stderr[-1000:] or "Codex repair worker failed",))
        if not changed_artifact:
            return WorkerResult(False, "no_code", completed.stdout[-4000:], evidence=("Codex repair returned without an implementation artifact or changed files",))
        return WorkerResult(True, "success", completed.stdout[-4000:], changed, evidence=(f"Codex changed {len(changed)} file(s)",))

    def _run_repository_tests(self, request: ExecutionRequest) -> VerificationResult:
        completed = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=request.worktree_path, capture_output=True, text=True)
        return VerificationResult(completed.returncode == 0, (completed.stdout[-2000:] or completed.stderr[-2000:],))

    def _codex_schema_path(self, gate: str) -> Path:
        path = self.state.state_dir / f"codex-{gate}-output.schema.json"
        if not path.exists():
            path.write_text(json.dumps(self.CODEX_GATE_SCHEMA, indent=2) + "\n", encoding="utf-8")
        return path

    def _codex_exec_argv(self, command: str, worktree: str, *, gate: str | None = None) -> list[str]:
        parts = shlex.split(command)
        if not parts:
            raise ValueError("Codex command is empty")
        if "exec" not in parts[1:]:
            parts.insert(1, "exec")
        parts.extend(["--cd", worktree])
        if gate is not None:
            parts.extend(["--output-schema", str(self._codex_schema_path(gate))])
        return parts

    @staticmethod
    def _parse_codex_result(output: str, gate: str) -> VerificationResult:
        try:
            payload = json.loads(output.strip())
        except (json.JSONDecodeError, TypeError):
            return VerificationResult(False, (f"Codex {gate} returned non-JSON output",), "review_unavailable")
        if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
            return VerificationResult(False, (f"Codex {gate} JSON must contain boolean passed",), "review_unavailable")
        evidence = payload.get("evidence", [])
        findings = payload.get("findings", [])
        if isinstance(evidence, str):
            evidence = [evidence]
        if isinstance(findings, str):
            findings = [findings]
        return VerificationResult(
            payload["passed"], tuple(str(item) for item in [*findings, *evidence]),
            "rejected_code" if not payload["passed"] else "approved",
        )

    def _run_codex_gate(self, gate, request, worker_result, verification):
        variable = "CODEX_REVIEW_COMMAND" if gate == "review" else "CODEX_FINAL_VERIFY_COMMAND"
        command = os.environ.get(variable) or os.environ.get("CODEX_COMMAND")
        if not command:
            return VerificationResult(False, (f"{variable} is not configured; {gate} failed closed",), "review_unavailable")
        prompt = json.dumps({
            "gate": gate, "request": asdict(request), "worker": asdict(worker_result),
            "verification": asdict(verification),
            "output_schema": {"passed": "boolean", "findings": ["string"], "evidence": ["string"]},
            "instruction": "Inspect the current worktree and emit only JSON matching the schema. passed must be false when findings remain.",
        }, indent=2, sort_keys=True)
        try:
            completed = subprocess.run(
                self._codex_exec_argv(
                    command, request.worktree_path, gate=gate
                ),
                cwd=request.worktree_path, capture_output=True, text=True,
                input=prompt,
                timeout=self.codex_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(False, (f"Codex {gate} timed out",), "review_unavailable")
        except OSError as exc:
            return VerificationResult(False, (f"Codex {gate} process could not be started: {exc}",), "review_unavailable")
        if completed.returncode != 0:
            return VerificationResult(False, (f"Codex {gate} process failed", completed.stderr[-1000:]), "review_unavailable")
        return self._parse_codex_result(completed.stdout, gate)

    def _run_codex_review(self, request, worker_result, tests):
        return self._run_codex_gate("review", request, worker_result, tests)

    def _run_codex_final_verification(self, request, worker_result, review):
        return self._run_codex_gate("final", request, worker_result, review)

    def _persistent(self) -> dict[str, Any]:
        state = self.state.get_registry().get(self.STATE_KEY, {})
        return state if isinstance(state, dict) else {}

    def _checkpoint(self, issue_id: str, phase: str, **values: Any) -> None:
        def mutate(registry, ledger):
            record = registry.setdefault(self.STATE_KEY, {"items": {}}).setdefault("items", {}).setdefault(issue_id, {})
            record.update({"phase": phase, "updated_at": _now(), **values})
            return registry, ledger
        self.state.update_state(mutate)

    def _begin(self, item_id: str, attempt: int, request: ExecutionRequest) -> None:
        self._checkpoint(
            item_id, "executing", attempts=attempt, status=BacklogStatus.IN_PROGRESS.value,
            selected_model=request.selected_model, workflow=request.workflow,
            request=asdict(request), owner_pid=os.getpid(),
        )

    def _recover_interrupted(self, items: list[BacklogItem], active: BacklogItem, record: Mapping[str, Any]) -> ExecutionOutcome:
        phase = str(record.get("phase", "claim"))
        reason = f"interrupted execution recovered from phase={phase}"
        self.runner.record_outcome(items, active.issue_id, BacklogStatus.BLOCKED, reason=reason)
        self._checkpoint(active.issue_id, "recovery_required", status=BacklogStatus.BLOCKED.value, recovery_reason=reason)
        return ExecutionOutcome(active.issue_id, BacklogStatus.BLOCKED.value, int(record.get("attempts", 0)), str(record.get("selected_model", "unknown")), (reason,))

    def _in_progress_outcome(self, items: list[BacklogItem]) -> ExecutionOutcome | None:
        active = next((item for item in items if item.status == BacklogStatus.IN_PROGRESS), None)
        if active is None:
            return None
        record = self._persistent().get("items", {}).get(active.issue_id, {})
        owner_pid = record.get("owner_pid")
        owner_alive = False
        if isinstance(owner_pid, int):
            try:
                os.kill(owner_pid, 0)
                owner_alive = True
            except OSError:
                owner_alive = False
        if record.get("phase") != "executing" or not owner_alive:
            return self._recover_interrupted(items, active, record)
        return ExecutionOutcome(active.issue_id, BacklogStatus.IN_PROGRESS.value, int(record.get("attempts", 0)), str(record.get("selected_model", "unknown")), ("existing IN_PROGRESS item inspected; worker was not re-dispatched",))

    @staticmethod
    def _provider(model: str) -> str:
        if model.startswith("p40"):
            return "p40"
        if model.startswith("gtx"):
            return "gtx"
        return model

    def _record_candidate(self, request, classification, *, review, evidence, final_decision):
        is_codex = request.selected_model == "codex"
        return self.dispatcher.record_attempt_result(
            goal_id=request.issue_id, goal_class="coding", model_id=request.selected_model,
            provider=self._provider(request.selected_model), classification=classification,
            credit_status="unknown", reviewer=None if is_codex else "codex",
            review_outcome=(
                "approved" if review.passed
                else "rejected" if review.failure_kind == "rejected_code"
                else "unavailable"
            ),
            reviewer_evidence="; ".join(review.evidence) if review.evidence else None,
            final_decision=final_decision, attempt_role="repair" if is_codex else "candidate",
            registry=self.state.get_registry(), evidence=list(evidence), workflow=request.workflow,
        )

    def _request_from_record(self, item: BacklogItem, record: Mapping[str, Any]) -> ExecutionRequest:
        request_data = record.get("request")
        if isinstance(request_data, dict):
            values = dict(request_data)
            values["acceptance_criteria"] = tuple(values.get("acceptance_criteria", ()))
            return ExecutionRequest(**values)
        return ExecutionRequest(
            item.issue_id, item.title, item.description, tuple(item.acceptance_criteria),
            str(self.repository_path), str(self.worktree_factory(item)), item.context_size,
            item.quality_importance, item.requires_benchmark_evidence, self.file_guidance,
            self.workflow, str(record.get("selected_model", "p40_qwen35")),
        )

    @staticmethod
    def _gate_exception(exc: Exception, gate: str) -> VerificationResult:
        return VerificationResult(
            False, (f"Codex {gate} gate raised {type(exc).__name__}: {exc}",),
            "review_unavailable",
        )

    def _pending_outcome(self, item: BacklogItem, record: Mapping[str, Any], evidence: list[str]) -> ExecutionOutcome:
        return ExecutionOutcome(
            item.issue_id, BacklogStatus.BLOCKED.value, int(record.get("attempts", 0)),
            str(record.get("selected_model", "p40_qwen35")), tuple(evidence),
        )

    def _resume_final_verification_pending(
        self, items: list[BacklogItem], item: BacklogItem, record: Mapping[str, Any],
    ) -> ExecutionOutcome:
        request = self._request_from_record(item, record)
        worker_result = _result(record.get("worker_result", {}))
        review = _verification(record.get("review", {}))
        evidence = list(record.get("evidence", ()))
        try:
            final = _verification(self.final_verifier(request, worker_result, review))
        except Exception as exc:
            final = self._gate_exception(exc, "final verification")
        evidence.extend(final.evidence)
        # Keep the resumable phase while downstream dispatcher/queue writes
        # are still outstanding.  A failure below must not strand the item
        # in a non-resumable ``*_complete`` phase.
        self._checkpoint(
            item.issue_id, "final_verification_pending", status=BacklogStatus.BLOCKED.value,
            classification="success", final_verification=asdict(final), evidence=evidence,
        )
        if not final.passed and final.failure_kind != "rejected_code":
            self._checkpoint(
                item.issue_id, "final_verification_pending", status=BacklogStatus.BLOCKED.value,
                classification="success", evidence=evidence,
            )
            return self._pending_outcome(item, record, evidence + ["candidate preserved pending Codex final verification"])

        final_registry = self.dispatcher.record_final_verification(
            goal_id=request.issue_id, goal_class="coding", provider="codex",
            classification="success" if final.passed else "rejected_code",
            review_outcome="approved" if final.passed else "rejected",
            reviewer_evidence="; ".join(final.evidence), evidence=evidence,
            registry=self.state.get_registry(), workflow=request.workflow,
        )
        if final.passed:
            status = BacklogStatus.COMPLETED
        else:
            next_model = str(final_registry.get("coding_dispatcher", {}).get("selected_model", ""))
            status = BacklogStatus.QUEUED if next_model in self.executors else BacklogStatus.BLOCKED
            if status == BacklogStatus.BLOCKED:
                evidence.append(f"next dispatcher selection has no executor: {next_model or 'none'}")
        self.runner.record_outcome(items, item.issue_id, status, reason="success" if final.passed else "rejected_code")
        phase = "completed" if status == BacklogStatus.COMPLETED else "queued" if status == BacklogStatus.QUEUED else "blocked"
        self._checkpoint(item.issue_id, phase, status=status.value, classification="success", evidence=evidence)
        return ExecutionOutcome(item.issue_id, status.value, int(record.get("attempts", 0)), request.selected_model, tuple(evidence))

    def _resume_review_pending(
        self, items: list[BacklogItem], item: BacklogItem, record: Mapping[str, Any],
    ) -> ExecutionOutcome:
        request = self._request_from_record(item, record)
        worker_result = _result(record.get("worker_result", {}))
        tests = _verification(record.get("tests", {}))
        evidence = list(record.get("evidence", ()))
        try:
            review = _verification(self.reviewer(request, worker_result, tests))
        except Exception as exc:
            review = self._gate_exception(exc, "review")
        evidence.extend(review.evidence)
        # The gate result is a checkpoint, but the phase remains pending until
        # candidate recording, final verification, and queue transition have
        # all completed durably.
        self._checkpoint(
            item.issue_id, "review_pending", status=BacklogStatus.BLOCKED.value,
            review=asdict(review), evidence=evidence,
        )
        if not review.passed and review.failure_kind == "review_unavailable":
            self._checkpoint(
                item.issue_id, "review_pending", status=BacklogStatus.BLOCKED.value,
                classification="candidate_pending_review", evidence=evidence,
            )
            return self._pending_outcome(item, record, evidence + ["candidate preserved pending Codex review"])

        if review.passed:
            classification = "success"
        else:
            classification = "incorrect_code" if review.failure_kind == "rejected_code" else review.failure_kind
        candidate_success = review.passed
        recorded = self._record_candidate(
            request, classification, review=review, evidence=evidence,
            final_decision="needs_fresh_codex_verification" if candidate_success else "rejected",
        )
        if candidate_success:
            self._checkpoint(
                item.issue_id, "final_verification_pending", status=BacklogStatus.BLOCKED.value,
                classification="success", request=asdict(request),
                worker_result=asdict(worker_result), tests=asdict(tests), review=asdict(review),
                candidate_recorded=True, evidence=evidence,
            )
        if not candidate_success:
            next_model = str(recorded.get("coding_dispatcher", {}).get("selected_model", ""))
            status = BacklogStatus.QUEUED if next_model in self.executors else BacklogStatus.BLOCKED
            if status == BacklogStatus.BLOCKED:
                evidence.append(f"next dispatcher selection has no executor: {next_model or 'none'}")
            self.runner.record_outcome(items, item.issue_id, status, reason=classification)
            phase = "queued" if status == BacklogStatus.QUEUED else "blocked"
            self._checkpoint(item.issue_id, phase, status=status.value, classification=classification, evidence=evidence)
            return ExecutionOutcome(item.issue_id, status.value, int(record.get("attempts", 0)), request.selected_model, tuple(evidence))

        try:
            final = _verification(self.final_verifier(request, worker_result, review))
        except Exception as exc:
            final = self._gate_exception(exc, "final verification")
        evidence.extend(final.evidence)
        self._checkpoint(item.issue_id, "final_verification_complete", final_verification=asdict(final), evidence=evidence)
        if not final.passed and final.failure_kind != "rejected_code":
            self._checkpoint(
                item.issue_id, "final_verification_pending", status=BacklogStatus.BLOCKED.value,
                classification="success", evidence=evidence,
            )
            return self._pending_outcome(item, record, evidence + ["candidate preserved pending Codex final verification"])
        final_registry = self.dispatcher.record_final_verification(
            goal_id=request.issue_id, goal_class="coding", provider="codex",
            classification="success" if final.passed else "rejected_code",
            review_outcome="approved" if final.passed else "rejected",
            reviewer_evidence="; ".join(final.evidence), evidence=evidence,
            registry=recorded, workflow=request.workflow,
        )
        if final.passed:
            status = BacklogStatus.COMPLETED
        else:
            next_model = str(final_registry.get("coding_dispatcher", {}).get("selected_model", ""))
            status = BacklogStatus.QUEUED if next_model in self.executors else BacklogStatus.BLOCKED
            if status == BacklogStatus.BLOCKED:
                evidence.append(f"next dispatcher selection has no executor: {next_model or 'none'}")
        self.runner.record_outcome(items, item.issue_id, status, reason=classification)
        phase = "completed" if status == BacklogStatus.COMPLETED else "queued" if status == BacklogStatus.QUEUED else "blocked"
        self._checkpoint(item.issue_id, phase, status=status.value, classification=classification, evidence=evidence)
        return ExecutionOutcome(item.issue_id, status.value, int(record.get("attempts", 0)), request.selected_model, tuple(evidence))

    def _resume_pending_verification(self) -> ExecutionOutcome | None:
        items = self._items()
        records = self._persistent().get("items", {})
        if not isinstance(records, dict):
            return None
        for item in items:
            record = records.get(item.issue_id, {})
            phase = record.get("phase") if isinstance(record, dict) else None
            if phase not in {"review_pending", "final_verification_pending"}:
                continue
            if phase == "review_pending":
                return self._resume_review_pending(items, item, record)
            return self._resume_final_verification_pending(items, item, record)
        return None

    def run_next(self) -> ExecutionOutcome | None:
        try:
            pending = self._resume_pending_verification()
            if pending is not None:
                return pending
            return self._run_next_once()
        except Exception as exc:
            try:
                return self._handle_pending_exception(exc)
            except Exception:
                return self._handle_unexpected_exception(exc)

    def _handle_pending_exception(self, exc: Exception) -> ExecutionOutcome:
        """Keep a blocked gate resumable when downstream persistence fails."""
        items = self._items()
        records = self._persistent().get("items", {})
        if not isinstance(records, dict):
            return self._handle_unexpected_exception(exc)
        for item in items:
            record = records.get(item.issue_id, {})
            phase = record.get("phase") if isinstance(record, dict) else None
            if phase not in {"review_pending", "final_verification_pending"}:
                continue
            evidence = list(record.get("evidence", ()))
            evidence.append(f"resume persistence failed: {type(exc).__name__}: {exc}")
            try:
                self._checkpoint(
                    item.issue_id, phase, status=BacklogStatus.BLOCKED.value,
                    evidence=evidence, recovery_error=str(exc),
                )
            except Exception:
                # The prior pending checkpoint is still the safest durable
                # state available; do not fall through and claim new work.
                pass
            return self._pending_outcome(item, record, evidence)
        return self._handle_unexpected_exception(exc)

    def _handle_unexpected_exception(self, exc: Exception) -> ExecutionOutcome:
        """Persist a provider/orchestrator exception before returning.

        This guard is deliberately outside the worker and verification calls:
        an invalid command, failing test process, or broken review wrapper must
        never leave the atomic queue in a live-looking ``executing`` state.
        """
        items = self._items()
        active = next((item for item in items if item.status == BacklogStatus.IN_PROGRESS), None)
        if active is None:
            raise exc
        record = self._persistent().get("items", {}).get(active.issue_id, {})
        selected_model = str(record.get("selected_model", "p40_qwen35"))
        classification = "endpoint_unavailable" if isinstance(exc, (FileNotFoundError, PermissionError)) else "unknown"
        evidence = [f"{type(exc).__name__}: {exc}", "execution exception was durably classified"]
        try:
            request_data = record.get("request")
            if isinstance(request_data, dict):
                request_data = dict(request_data)
                request_data["acceptance_criteria"] = tuple(request_data.get("acceptance_criteria", ()))
                request = ExecutionRequest(**request_data)
            else:
                request = ExecutionRequest(
                    active.issue_id, active.title, active.description,
                    tuple(active.acceptance_criteria), str(self.repository_path),
                    str(self.repository_path / ".worktrees" / active.issue_id),
                    active.context_size, active.quality_importance,
                    active.requires_benchmark_evidence, self.file_guidance,
                    self.workflow, selected_model,
                )
            recorded = self._record_candidate(
                request, classification,
                review=VerificationResult(False, tuple(evidence)),
                evidence=evidence, final_decision="rejected",
            )
            next_model = str(recorded.get("coding_dispatcher", {}).get("selected_model", ""))
            status = BacklogStatus.QUEUED if next_model in self.executors else BacklogStatus.BLOCKED
            if status == BacklogStatus.BLOCKED:
                evidence.append(f"next dispatcher selection has no executor: {next_model or 'none'}")
        except Exception as record_exc:
            status = BacklogStatus.BLOCKED
            evidence.append(f"could not persist dispatcher outcome: {record_exc}")
        self.runner.record_outcome(items, active.issue_id, status, reason=classification)
        self._checkpoint(
            active.issue_id, "exception", status=status.value,
            selected_model=selected_model, classification=classification,
            evidence=evidence,
        )
        return ExecutionOutcome(
            active.issue_id, status.value, int(record.get("attempts", 0)),
            selected_model, tuple(evidence),
        )

    def _run_next_once(self) -> ExecutionOutcome | None:
        items, dispatch = self.runner.dispatch_next(self._dispatch_route, workflow=self.workflow)
        if dispatch is None:
            return self._in_progress_outcome(items)
        if dispatch.status == BacklogStatus.BLOCKED:
            return ExecutionOutcome(dispatch.item_id, BacklogStatus.BLOCKED.value, 0, "dispatcher", (f"routing failed: {dispatch.routing.get('error', 'unknown error')}",))
        item = next(item for item in items if item.issue_id == dispatch.item_id)
        request = self._request(item, dispatch.routing)
        executor = self.executors.get(request.selected_model)
        if executor is None:
            reason = f"no executor configured for selected model {request.selected_model}"
            self.runner.record_outcome(items, item.issue_id, BacklogStatus.BLOCKED, reason=reason)
            self._checkpoint(item.issue_id, "executor_missing", status=BacklogStatus.BLOCKED.value, selected_model=request.selected_model, recovery_reason=reason)
            return ExecutionOutcome(item.issue_id, BacklogStatus.BLOCKED.value, 0, request.selected_model, (reason,))

        prior = self._persistent().get("items", {}).get(item.issue_id, {})
        attempt = int(prior.get("attempts", 0)) + 1
        self._begin(item.issue_id, attempt, request)
        evidence = [f"worktree={request.worktree_path}", f"workflow={request.workflow}", f"executor={request.selected_model}"]
        worker_result = _result(executor(request))
        if worker_result.success and not worker_result.changed_files:
            worker_result = replace(worker_result, success=False, classification="no_code", evidence=worker_result.evidence + ("worker reported success without changed-file/artifact evidence",))
        evidence.extend(worker_result.evidence)
        self._checkpoint(item.issue_id, "worker_complete", worker_result=asdict(worker_result))

        tests = _verification(self.test_runner(request)) if worker_result.success else VerificationResult(False, ("worker did not produce a successful artifact",))
        evidence.extend(tests.evidence)
        self._checkpoint(item.issue_id, "tests_complete", tests=asdict(tests))
        self_review = VerificationResult(True, ("P40 self-review not required",), "approved")
        self_review_mutated = False
        if request.selected_model.startswith("p40") and worker_result.success and tests.passed:
            before_head, before_fingerprint, before_files = self._git_snapshot(request.worktree_path)
            try:
                self_review = _verification(self.self_reviewer(request, worker_result, tests))
            except Exception as exc:
                self_review = VerificationResult(
                    False, (f"P40 self-review raised {type(exc).__name__}: {exc}",),
                    "review_unavailable",
                )
            after_head, after_fingerprint, after_files = self._git_snapshot(request.worktree_path)
            if before_head != after_head or before_fingerprint != after_fingerprint:
                self_review = VerificationResult(
                    False,
                    (
                        "P40 self-review modified the worktree; review was rejected",
                        f"before files={before_files}", f"after files={after_files}",
                    ),
                    "self_review_mutated",
                )
            evidence.extend(self_review.evidence)
            self_review_mutated = self_review.failure_kind == "self_review_mutated"
            # Self-review is preliminary only. Preserve its findings in the
            # worker evidence so Codex receives the same risks independently.
            worker_result = replace(
                worker_result,
                evidence=worker_result.evidence + self_review.evidence,
            )
            if self_review_mutated:
                worker_result = replace(
                    worker_result, success=False, classification="tests_failed",
                )
            self._checkpoint(item.issue_id, "self_review_complete", self_review=asdict(self_review))
        should_review = (
            not self_review_mutated
            and (request.selected_model == "codex" or worker_result.success or worker_result.classification in SUBSTANTIVE)
        )
        review = (
            _verification(self.reviewer(request, worker_result, tests))
            if should_review
            else VerificationResult(
                False,
                ("Codex review skipped because self-review changed the tested worktree",)
                if self_review_mutated else ("review gate not reached",),
                "rejected_code" if self_review_mutated else "rejected_code",
            )
        )
        evidence.extend(review.evidence)
        self._checkpoint(item.issue_id, "review_complete", self_review=asdict(self_review), review=asdict(review))

        # A valid implementation must not be sent back to P40 merely because
        # the Codex review service is unavailable. Preserve the candidate and
        # block pending review; a later verifier can resume this phase without
        # invoking the implementation executor again.
        implementation_ready = worker_result.success and tests.passed
        if implementation_ready and not review.passed and review.failure_kind == "review_unavailable":
            self.runner.record_outcome(
                items, item.issue_id, BacklogStatus.BLOCKED,
                reason="review_unavailable",
            )
            self._checkpoint(
                item.issue_id, "review_pending",
                status=BacklogStatus.BLOCKED.value,
                classification="candidate_pending_review", evidence=evidence,
                request=asdict(request), worker_result=asdict(worker_result),
                tests=asdict(tests), self_review=asdict(self_review), review=asdict(review),
            )
            return ExecutionOutcome(
                item.issue_id, BacklogStatus.BLOCKED.value, attempt,
                request.selected_model,
                tuple(evidence + ["candidate preserved pending Codex review"]),
            )

        if not worker_result.success:
            classification = worker_result.classification
        elif not tests.passed:
            classification = "tests_failed"
        elif not review.passed:
            classification = (
                "incorrect_code"
                if review.failure_kind == "rejected_code"
                else review.failure_kind
            )
        else:
            classification = "success"
        candidate_success = worker_result.success and tests.passed and review.passed
        final = VerificationResult(False, ("final verification gate not reached",))
        if candidate_success:
            final = _verification(self.final_verifier(request, worker_result, review))
            evidence.extend(final.evidence)
            self._checkpoint(item.issue_id, "final_verification_complete", final_verification=asdict(final))
        recorded = self._record_candidate(
            request, classification, review=review, evidence=evidence,
            final_decision="needs_fresh_codex_verification" if candidate_success else "rejected",
        )
        pending_verification = (
            candidate_success and not final.passed
            and final.failure_kind != "rejected_code"
        )
        if candidate_success and not pending_verification:
            final_registry = self.dispatcher.record_final_verification(
                goal_id=request.issue_id, goal_class="coding", provider="codex",
                classification="success" if final.passed else "rejected_code",
                review_outcome="approved" if final.passed else "rejected",
                reviewer_evidence="; ".join(final.evidence), evidence=list(evidence),
                registry=recorded, workflow=request.workflow,
            )
        else:
            final_registry = recorded
        if candidate_success and final.passed:
            status = BacklogStatus.COMPLETED
        elif pending_verification:
            status = BacklogStatus.BLOCKED
            evidence.append("candidate preserved pending Codex final verification")
        elif any("failed closed" in entry or "not configured" in entry for entry in evidence):
            status = BacklogStatus.BLOCKED
            evidence.append("verification infrastructure is unavailable; item is blocked")
        else:
            next_model = str(final_registry.get("coding_dispatcher", {}).get("selected_model", ""))
            status = BacklogStatus.QUEUED if next_model in self.executors else BacklogStatus.BLOCKED
            if status == BacklogStatus.BLOCKED:
                evidence.append(f"next dispatcher selection has no executor: {next_model or 'none'}")
        self.runner.record_outcome(items, item.issue_id, status, reason=classification)
        phase = (
            "completed" if status == BacklogStatus.COMPLETED
            else "queued" if status == BacklogStatus.QUEUED
            else "final_verification_pending" if pending_verification
            else "blocked"
        )
        self._checkpoint(item.issue_id, phase, status=status.value, classification=classification, evidence=evidence)
        return ExecutionOutcome(item.issue_id, status.value, attempt, request.selected_model, tuple(evidence))
