"""Durable, stage-only Telegram image ingress for compute01."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .scheduler.storage import StorageContract


DEFAULT_STORAGE_ROOT = Path("/mnt/scratch/gtx-images")


@dataclass(frozen=True)
class StagingResult:
    accepted: bool
    status: str
    task_id: str | None = None
    path: str | None = None
    error: str | None = None
    idempotent_replay: bool = False


def _reject(error: str) -> StagingResult:
    return StagingResult(False, "rejected", error=error)


@contextmanager
def _storage_lock(storage_root: Path) -> Iterator[None]:
    """Serialize task publication so deterministic replays cannot race."""
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".telegram-image-ingress.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _remove_incomplete_publication(storage: StorageContract, task_id: str) -> None:
    """Remove only an incomplete task published by this stage-only boundary."""
    task_dir = storage.incoming_path / task_id
    if task_dir.is_dir() and not task_dir.is_symlink():
        shutil.rmtree(task_dir)


def _existing_result(storage: StorageContract, task_id: str) -> StagingResult | None:
    metadata = storage.get_task(task_id)
    path = storage.input_path(task_id)
    if metadata is None or path is None or not path.is_file():
        return None

    status = metadata.get("status")
    if status == "accepted":
        repaired = storage.update_metadata(
            task_id,
            {"status": "saved", "ingress_mode": "telegram-stage-only"},
        )
        metadata = storage.get_task(task_id)
        if not repaired or metadata is None or metadata.get("status") != "saved":
            _remove_incomplete_publication(storage, task_id)
            return _reject("existing staging record is incomplete")
        status = metadata.get("status")
    if status != "saved":
        return _reject(f"existing staging record has unexpected status: {status!r}")

    return StagingResult(
        True,
        "saved",
        task_id=task_id,
        path=str(path),
        idempotent_replay=True,
    )


def stage_event(event: Mapping[str, Any], storage_root: Path) -> StagingResult:
    """Validate and atomically publish one Telegram image under ``incoming``.

    This boundary deliberately does not create a scheduler task or start model
    work. It only makes the image durable on compute01 for later processing.
    """
    if not isinstance(event, Mapping):
        return _reject("event must be a JSON object")

    source_value = event.get("source_path")
    if not isinstance(source_value, str) or not source_value.strip():
        return _reject("source_path is required")
    source_path = Path(source_value).expanduser()
    if not source_path.is_file() or source_path.is_symlink():
        return _reject("source_path must be an existing regular, non-symlink file")

    storage_root = Path(storage_root).expanduser()
    try:
        with _storage_lock(storage_root):
            storage = StorageContract(storage_root)
            valid, error, metadata = storage.validate_input(source_path)
            if not valid or metadata is None:
                return _reject(error or "input validation failed")

            source_chat = event.get("chat_id")
            source_message = event.get("message_id")
            idempotency_key = None
            if source_chat is not None and source_message is not None:
                idempotency_key = storage.generate_idempotency_key(
                    str(source_chat), str(source_message), metadata["content_hash"]
                )
            task_id = storage.generate_task_id(idempotency_key=idempotency_key)
            existing = _existing_result(storage, task_id)
            if existing is not None:
                return existing

            task_id, _ = storage.stage_input(
                source_path,
                source_chat=str(source_chat) if source_chat is not None else None,
                source_message_id=(
                    str(source_message) if source_message is not None else None
                ),
                idempotency_key=idempotency_key,
            )
            if not storage.update_metadata(
                task_id,
                {
                    "status": "saved",
                    "caption": event.get("caption"),
                    "media_group_id": event.get("media_group_id"),
                    "source_kind": event.get("kind"),
                    "ingress_mode": "telegram-stage-only",
                },
            ):
                _remove_incomplete_publication(storage, task_id)
                return _reject("could not persist staging metadata")
            path = storage.input_path(task_id)
            if path is None or not path.is_file():
                _remove_incomplete_publication(storage, task_id)
                return _reject("staged image path is missing")
            return StagingResult(True, "saved", task_id=task_id, path=str(path))
    except (OSError, ValueError, TypeError) as exc:
        return _reject(str(exc))


def main(argv: list[str] | None = None) -> int:
    """Read one Hermes event from stdin and emit one JSON staging result."""
    if argv is None:
        argv = sys.argv[1:]
    if argv != ["--json-stdin"]:
        print("usage: paperclip-image-ingress --json-stdin", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result = _reject(f"invalid JSON: {exc}")
    else:
        result = stage_event(
            event,
            Path(os.environ.get("GTX_IMAGE_STORAGE_ROOT", DEFAULT_STORAGE_ROOT)),
        )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.accepted else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
