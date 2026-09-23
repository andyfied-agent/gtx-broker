"""Durable, process-safe state for the Second Shift registry and ledger."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


class StateFile:
    """Persist registry and ledger data with a stable transaction lock.

    The lock is a sidecar inode and is never replaced by an atomic data-file
    rename. ``update_state`` performs the read/modify/write under one blocking
    exclusive lock, so concurrent attempts cannot lose updates.
    """

    SUBSTANTIVE_FAILURE_THRESHOLD = 3
    NON_SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "no_code", "timeout", "endpoint_unavailable", "authentication",
        "credit_exhausted", "rate_limited", "policy_refusal", "context_limit",
        "review_unavailable", "unknown",
    })
    SUBSTANTIVE_CLASSIFICATIONS = frozenset({
        "partial_code", "incorrect_code", "tests_failed",
    })

    def __init__(self, state_dir: Optional[str] = None):
        if state_dir is None:
            state_dir = os.path.join(
                os.environ.get("HOME", os.path.expanduser("~")),
                ".hermes", "second-shift",
            )
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.state_dir / "registry.json"
        self.ledger_path = self.state_dir / "ledger.jsonl"
        self.lock_path = self.state_dir / "state.lock"

    def _lock_path(self, _path: Path) -> Path:
        """Return the stable inode used for all state-file locking."""
        return self.lock_path

    def _acquire_lock(self, path: Path, mode: str = "r", *, blocking: bool = False) -> Optional[int]:
        """Acquire a stable sidecar lock.

        The non-blocking default is retained for legacy inspection tests;
        production reads/writes use ``blocking=True`` through ``_locked``.
        """
        fd = None
        try:
            fd = os.open(self._lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
            lock_type = fcntl.LOCK_SH if mode == "r" else fcntl.LOCK_EX
            if not blocking:
                lock_type |= fcntl.LOCK_NB
            fcntl.flock(fd, lock_type)
            return fd
        except OSError:
            if fd is not None:
                os.close(fd)
            return None

    def _release_lock(self, fd: Optional[int]) -> None:
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self, mode: str = "r"):
        fd = self._acquire_lock(self.registry_path, mode=mode, blocking=True)
        if fd is None:
            raise OSError("unable to acquire Second Shift state lock")
        try:
            yield
        finally:
            self._release_lock(fd)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return copy.deepcopy(default)
        text = path.read_text()
        if not text.strip():
            return copy.deepcopy(default)
        return json.loads(text)

    def _read_registry_unlocked(self) -> dict:
        value = self._read_json(self.registry_path, {})
        return value if isinstance(value, dict) else {}

    def _read_ledger_unlocked(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        entries = []
        for line in self.ledger_path.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _write_registry_unlocked(self, registry: dict) -> None:
        self._atomic_write(
            self.registry_path,
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
        )

    def _write_ledger_unlocked(self, entries: list[dict]) -> None:
        text = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)
        self._atomic_write(self.ledger_path, text)

    def update_state(self, mutator: Callable[[dict, list[dict]], tuple[dict, list[dict]]]):
        """Atomically read, transform, and persist registry plus ledger."""
        with self._locked("w"):
            registry = self._read_registry_unlocked()
            entries = self._read_ledger_unlocked()
            new_registry, new_entries = mutator(
                copy.deepcopy(registry), copy.deepcopy(entries)
            )
            if not isinstance(new_registry, dict) or not isinstance(new_entries, list):
                raise TypeError("state mutator must return (dict, list)")
            self._write_registry_unlocked(new_registry)
            self._write_ledger_unlocked(new_entries)
            return copy.deepcopy(new_registry)

    def get_registry(self) -> dict:
        with self._locked("r"):
            return self._read_registry_unlocked()

    def save_registry(self, registry: dict) -> None:
        with self._locked("w"):
            self._write_registry_unlocked(copy.deepcopy(registry))

    def append_ledger_entry(self, entry: dict) -> None:
        with self._locked("w"):
            entries = self._read_ledger_unlocked()
            entries.append(copy.deepcopy(entry))
            self._write_ledger_unlocked(entries)

    def get_ledger_entries(self) -> list[dict]:
        with self._locked("r"):
            return self._read_ledger_unlocked()

    def save_ledger_entries(self, entries: list[dict]) -> None:
        with self._locked("w"):
            self._write_ledger_unlocked(copy.deepcopy(entries))

    def clear_ledger(self) -> None:
        with self._locked("w"):
            self._write_ledger_unlocked([])

    def timestamp_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
