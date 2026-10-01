"""Admission contract for bounded, review-gated repository tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from .repository_boundary import RepositoryRegistry, RepositoryPreflight


class TaskManifestError(ValueError):
    """Raised when a task manifest is incomplete or unsafe to dispatch."""


TASK_MANIFEST_VERSION = 1
MAX_CONTEXT_SIZE = 262_144
MAX_TIMEOUT_SECONDS = 7_200
ALLOWED_WORKERS = {"p40-coding", "slow-coder"}
MERGE_POLICY = "codex-review-then-air-failover"
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

REQUIRED_FIELDS = {
    "manifest_version",
    "task_id",
    "goal_id",
    "session_id",
    "repository",
    "repository_remote",
    "base_ref",
    "branch",
    "worktree_path",
    "context_documents",
    "scope",
    "acceptance_criteria",
    "test_command",
    "worker_profile",
    "timeout_seconds",
    "context_size",
    "commit_required",
    "pull_request_required",
    "review_policy",
    "merge_policy",
}


@dataclass(frozen=True)
class TaskManifest:
    """Validated manifest retained as immutable task-admission evidence."""

    values: dict[str, Any]
    preflight: RepositoryPreflight

    @property
    def task_id(self) -> str:
        return self.values["task_id"]

    @property
    def repository(self) -> str:
        return self.values["repository"]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


def _require_string(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TaskManifestError(f"{name} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise TaskManifestError(f"{name} must not contain control characters")
    return value.strip()


def _require_string_list(values: Mapping[str, Any], name: str) -> list[str]:
    value = values.get(name)
    if not isinstance(value, list) or not value:
        raise TaskManifestError(f"{name} must be a non-empty list")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise TaskManifestError(f"{name}[{index}] must be a non-empty string")
        result.append(item.strip())
    return result


def _validate_relative_documents(documents: list[str]) -> None:
    for document in documents:
        path = Path(document)
        if path.is_absolute() or ".." in path.parts:
            raise TaskManifestError(
                f"context document must be a repository-relative path: {document}"
            )


def validate_task_manifest(
    manifest: Mapping[str, Any],
    *,
    registry: Optional[RepositoryRegistry] = None,
    check_worktree: bool = True,
    context_limit: Optional[int] = None,
) -> TaskManifest:
    """Validate a coding manifest and its repository boundary.

    ``check_worktree=False`` is intended only for schema/unit tests. Production
    admission must leave it enabled so the Git identity and clean branch are
    checked immediately before dispatch.
    """
    if not isinstance(manifest, Mapping):
        raise TaskManifestError("task manifest must be an object")
    values = dict(manifest)
    missing = sorted(REQUIRED_FIELDS - values.keys())
    if missing:
        raise TaskManifestError(f"missing required field(s): {', '.join(missing)}")
    unknown = sorted(set(values) - REQUIRED_FIELDS)
    if unknown:
        raise TaskManifestError(f"unknown field(s): {', '.join(unknown)}")

    if values["manifest_version"] != TASK_MANIFEST_VERSION:
        raise TaskManifestError(
            f"manifest_version must be {TASK_MANIFEST_VERSION}"
        )
    for name in ("task_id", "goal_id", "session_id", "repository", "repository_remote", "base_ref", "branch", "worktree_path", "scope", "test_command", "worker_profile", "merge_policy"):
        values[name] = _require_string(values, name)
    if not Path(values["worktree_path"]).is_absolute():
        raise TaskManifestError("worktree_path must be absolute")
    if values["branch"] == "main" or values["branch"] == "master":
        raise TaskManifestError("branch must not be a default branch")
    if not BRANCH_PATTERN.fullmatch(values["branch"]):
        raise TaskManifestError("branch contains unsupported characters")
    if ".." in values["branch"] or values["branch"].startswith("/"):
        raise TaskManifestError("branch must not contain traversal or leading slash")

    values["context_documents"] = _require_string_list(values, "context_documents")
    _validate_relative_documents(values["context_documents"])
    values["acceptance_criteria"] = _require_string_list(values, "acceptance_criteria")
    if not isinstance(values["timeout_seconds"], (int, float)) or isinstance(values["timeout_seconds"], bool):
        raise TaskManifestError("timeout_seconds must be numeric")
    if not 1 <= values["timeout_seconds"] <= MAX_TIMEOUT_SECONDS:
        raise TaskManifestError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    if not isinstance(values["context_size"], int) or isinstance(values["context_size"], bool):
        raise TaskManifestError("context_size must be an integer")
    if not 1 <= values["context_size"] <= MAX_CONTEXT_SIZE:
        raise TaskManifestError(f"context_size must be between 1 and {MAX_CONTEXT_SIZE}")
    if context_limit is not None and values["context_size"] > context_limit:
        raise TaskManifestError(
            f"context_size {values['context_size']} exceeds worker limit {context_limit}"
        )
    if values["worker_profile"] not in ALLOWED_WORKERS:
        raise TaskManifestError(f"worker_profile must be one of {sorted(ALLOWED_WORKERS)}")
    for name in ("commit_required", "pull_request_required"):
        if values[name] is not True:
            raise TaskManifestError(f"{name} must be true for automated coding work")
    if values["merge_policy"] != MERGE_POLICY:
        raise TaskManifestError(f"merge_policy must be {MERGE_POLICY!r}")
    policy = values["review_policy"]
    if not isinstance(policy, Mapping):
        raise TaskManifestError("review_policy must be an object")
    required_policy = {
        "codex_review_required": True,
        "air_review_failover": True,
        "merge_on_approval": True,
    }
    if dict(policy) != required_policy:
        raise TaskManifestError(
            "review_policy must require Codex review, Air Review failover, and approval merge"
        )
    values["review_policy"] = dict(policy)

    registry = registry or RepositoryRegistry.compute01_defaults()
    try:
        preflight = registry.validate_checkout(
            values["repository"],
            values["worktree_path"],
            expected_remote=values["repository_remote"],
        ) if check_worktree else _validate_declared_boundary(registry, values)
    except (OSError, ValueError) as exc:
        if isinstance(exc, TaskManifestError):
            raise
        raise TaskManifestError(str(exc)) from exc

    if check_worktree:
        missing_documents = [
            document for document in values["context_documents"]
            if not (preflight.worktree_path / document).is_file()
        ]
        if missing_documents:
            raise TaskManifestError(
                f"required context document(s) missing: {', '.join(missing_documents)}"
            )
        if preflight.branch != values["branch"]:
            raise TaskManifestError(
                f"manifest branch {values['branch']!r} does not match worktree branch {preflight.branch!r}"
            )
    return TaskManifest(values, preflight)


def _validate_declared_boundary(
    registry: RepositoryRegistry, values: Mapping[str, Any]
) -> RepositoryPreflight:
    """Validate schema/path/remote without requiring a live Git worktree."""
    repository = registry.get(values["repository"])
    path = Path(values["worktree_path"]).expanduser().resolve(strict=False)
    if not repository.contains(path):
        raise TaskManifestError(
            f"worktree is outside the owned roots for {repository.name}: {path}"
        )
    if not repository.matches_remote(values["repository_remote"]):
        raise TaskManifestError(
            f"manifest remote is not allowed for owned repository {repository.name}"
        )
    return RepositoryPreflight(
        repository, path, (values["repository_remote"],), values["branch"],
        values["repository_remote"],
    )
