"""Tests for repository ownership and task-manifest admission."""

import subprocess
import os

import pytest

from gtx_broker import (
    OwnedRepository,
    RepositoryBoundaryError,
    RepositoryRegistry,
    TaskManifestError,
    validate_task_manifest,
)
from gtx_broker.scheduler import Scheduler, SchedulerConfig
from gtx_broker.scheduler.handlers import CodingHandler, HandlerResult


def make_registry(tmp_path):
    return RepositoryRegistry((OwnedRepository(
        "demo",
        "https://github.com/example/demo.git",
        (tmp_path / "demo", tmp_path / "worktrees"),
    ),))


def make_manifest(tmp_path):
    return {
        "manifest_version": 1,
        "task_id": "TASK-001",
        "goal_id": "GOAL-001",
        "session_id": "SESSION-001",
        "repository": "demo",
        "repository_remote": "https://github.com/example/demo.git",
        "base_ref": "main",
        "branch": "automation/task-001",
        "worktree_path": str(tmp_path / "worktrees" / "task-001"),
        "context_documents": ["README.md", "INITIALISATION.md"],
        "scope": "Implement the bounded change described by the task.",
        "acceptance_criteria": ["The deterministic test command passes."],
        "test_command": "python -m pytest -q",
        "worker_profile": "p40-coding",
        "timeout_seconds": 1800,
        "context_size": 131072,
        "commit_required": True,
        "pull_request_required": True,
        "review_policy": {
            "codex_review_required": True,
            "air_review_failover": True,
            "merge_on_approval": True,
        },
        "merge_policy": "codex-review-then-air-failover",
    }


def test_valid_manifest_checks_declared_boundary_without_git(tmp_path):
    manifest = make_manifest(tmp_path)
    result = validate_task_manifest(
        manifest, registry=make_registry(tmp_path), check_worktree=False
    )
    assert result.task_id == "TASK-001"
    assert result.repository == "demo"


def test_manifest_rejects_unowned_repository_and_path(tmp_path):
    manifest = make_manifest(tmp_path)
    manifest["repository"] = "not-owned"
    with pytest.raises(TaskManifestError, match="repository is not owned"):
        validate_task_manifest(manifest, registry=make_registry(tmp_path), check_worktree=False)

    manifest = make_manifest(tmp_path)
    manifest["worktree_path"] = str(tmp_path / "outside")
    with pytest.raises(TaskManifestError, match="outside the owned roots"):
        validate_task_manifest(manifest, registry=make_registry(tmp_path), check_worktree=False)


@pytest.mark.parametrize("field", ["context_documents", "acceptance_criteria", "test_command"])
def test_manifest_rejects_incomplete_execution_contract(tmp_path, field):
    manifest = make_manifest(tmp_path)
    manifest[field] = [] if field != "test_command" else ""
    with pytest.raises(TaskManifestError):
        validate_task_manifest(manifest, registry=make_registry(tmp_path), check_worktree=False)


def test_manifest_rejects_unsafe_review_or_worker_policy(tmp_path):
    manifest = make_manifest(tmp_path)
    manifest["worker_profile"] = "air-review"
    with pytest.raises(TaskManifestError, match="worker_profile"):
        validate_task_manifest(manifest, registry=make_registry(tmp_path), check_worktree=False)

    manifest = make_manifest(tmp_path)
    manifest["merge_policy"] = "worker-decides"
    with pytest.raises(TaskManifestError, match="merge_policy"):
        validate_task_manifest(manifest, registry=make_registry(tmp_path), check_worktree=False)

    manifest = make_manifest(tmp_path)
    manifest["worker_profile"] = "slow-coder"
    manifest["context_size"] = 65537
    with pytest.raises(TaskManifestError, match="worker limit 65536"):
        validate_task_manifest(
            manifest,
            registry=make_registry(tmp_path),
            check_worktree=False,
            context_limit=65536,
        )


def test_manifest_task_executes_and_accepts_a_real_commit(tmp_path, monkeypatch):
    path = tmp_path / "worktrees" / "task-001"
    path.mkdir(parents=True)
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Boundary Tests")
    git("switch", "-c", "automation/task-001")
    for document in ("README.md", "INITIALISATION.md"):
        (path / document).write_text(f"# {document}\n")
    git("add", ".")
    git("commit", "-qm", "test fixture")
    git("remote", "add", "origin", "https://github.com/example/demo.git")

    manifest = make_manifest(tmp_path)
    manifest["worktree_path"] = str(path)
    manifest["test_command"] = "true"
    result = validate_task_manifest(manifest, registry=make_registry(tmp_path))
    assert result.preflight.branch == "automation/task-001"

    registry = make_registry(tmp_path)
    scheduler = Scheduler(
        SchedulerConfig(db_path=str(tmp_path / "tasks.db")),
        repository_registry=registry,
    )
    assert scheduler.add_manifest_task(manifest, registry=registry)
    queued = scheduler.get_task("TASK-001")
    assert queued["state"] == "queued"
    assert queued["payload"]["worker_profile"] == "p40-coding"
    assert queued["payload"]["timeout"] == 1800
    assert queued["payload"]["context_size"] == 131072
    assert scheduler.claim_task("TASK-001") is not None
    assert not scheduler.start_task("TASK-001", "slow-coder")
    assert scheduler.get_task("TASK-001")["state"] == "claimed"
    # The declared P40 worker is the only worker allowed to start this task.
    assert scheduler.start_task("TASK-001", "p40-coding")

    executor = tmp_path / "executor.sh"
    executor.write_text(
        "#!/bin/sh\n"
        "printf 'implemented\\n' > committed.txt\n"
        "git config user.email tests@example.invalid\n"
        "git config user.name 'Boundary Tests'\n"
        "git add committed.txt\n"
        "git commit -m 'implement manifest task'\n"
    )
    executor.chmod(executor.stat().st_mode | 0o111)
    monkeypatch.setenv("P40_CODING_COMMAND", str(executor))
    task = scheduler.get_task("TASK-001")
    task["worker_profile"] = "p40-coding"
    handler = CodingHandler()
    result = handler.execute(task)
    assert result == HandlerResult.AWAITING_REVIEW, handler.last_result
    assert scheduler.transition_running_to_awaiting_review("TASK-001")
    assert scheduler.get_task("TASK-001")["state"] == "awaiting_review"
    assert not scheduler.approve_awaiting_review("TASK-001")
    assert scheduler.get_task("TASK-001")["state"] == "awaiting_review"

    (path / "dirty.txt").write_text("must fail\n")
    with pytest.raises(TaskManifestError, match="clean"):
        validate_task_manifest(manifest, registry=make_registry(tmp_path))


def test_repository_registry_rejects_cross_repo_remote(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        registry.validate_checkout("demo", tmp_path / "demo", expected_remote="https://github.com/other/repo.git")


def test_repository_registry_rejects_unapproved_effective_push_remote(tmp_path):
    path = tmp_path / "demo"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True,
            capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Boundary Tests")
    git("switch", "-c", "automation/task-remote")
    (path / "README.md").write_text("# demo\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    git("remote", "add", "origin", "https://github.com/example/demo.git")
    git("remote", "add", "unapproved", "https://github.com/other/demo.git")
    git("config", "branch.automation/task-remote.remote", "origin")
    git("config", "branch.automation/task-remote.pushRemote", "unapproved")
    registry = RepositoryRegistry((OwnedRepository(
        "demo", "https://github.com/example/demo.git", (path,),
        ("https://github.com/example/demo.git",),
    ),))

    with pytest.raises(RepositoryBoundaryError, match="push remote"):
        registry.validate_checkout(
            "demo", path, expected_remote="https://github.com/example/demo.git"
        )
