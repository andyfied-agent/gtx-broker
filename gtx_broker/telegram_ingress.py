"""Retired Telegram task ingress kept as a fail-closed compatibility command."""

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .scheduler import Scheduler, SchedulerConfig, StorageContract
from .scheduler.handlers import VisionHandler
from .scheduler.quality import ImageQualityGate


@dataclass(frozen=True)
class IngressResult:
    accepted: bool
    status: str
    task_id: str | None = None
    path: str | None = None
    quality: dict | None = None
    error: str | None = None


class TelegramImageIngress:
    """Validate, persist, and enqueue one authorized Telegram image event.

    This library API is retained for historical compatibility.  The public CLI
    entry point is fail-closed because active Telegram staging uses the separate
    stage-only ingress boundary.
    """

    DISABLED_REASON = (
        "GTX Broker task ingress is retired; use the durable Telegram image staging command"
    )

    def __init__(self, scheduler: Scheduler, storage: StorageContract,
                 quality_gate: ImageQualityGate | None = None):
        self.scheduler = scheduler
        self.storage = storage
        self.quality_gate = quality_gate or ImageQualityGate()

    def _reject_staged_task(self, task_id: str | None, reason: str) -> None:
        """Leave a durable audit record and cancel a task created before failure."""
        if not task_id:
            return
        scheduler_task = self.scheduler.get_task(task_id)
        if scheduler_task and scheduler_task.get("state") in {"queued", "claimed"}:
            self.scheduler.cancel_task(task_id)
        if not self.storage.reject_task(task_id, reason):
            # The original exception is more useful to the caller than a second
            # storage error, but make the orphan risk visible to operators.
            import logging
            logging.getLogger(__name__).error(
                "Could not mark staged ingress task %s rejected", task_id
            )

    def ingest(self, event: Mapping[str, Any]) -> IngressResult:
        source_value = event.get("source_path")
        if not source_value:
            return IngressResult(False, "rejected", error="source_path is required")
        source_path = Path(str(source_value)).expanduser()
        valid, error, metadata = self.storage.validate_input(source_path)
        if not valid or metadata is None:
            return IngressResult(False, "rejected", error=error)
        schema = event.get("schema", event.get("output_schema", VisionHandler.RECEIPT_SCHEMA))
        if not isinstance(schema, str) or schema not in VisionHandler.SUPPORTED_SCHEMAS:
            return IngressResult(False, "rejected", error=f"unsupported vision schema: {schema!r}")
        requires_review = event.get("requires_review")
        if requires_review is None:
            requires_review = schema == VisionHandler.RECEIPT_SCHEMA
        if not isinstance(requires_review, bool):
            return IngressResult(False, "rejected", error="requires_review must be boolean")

        source_chat = event.get("chat_id")
        source_message = event.get("message_id")
        key = event.get("idempotency_key") or self.storage.generate_idempotency_key(
            str(source_chat or ""), str(source_message or ""), metadata["content_hash"]
        )
        task_id = self.storage.generate_task_id(idempotency_key=key)
        existing = self.scheduler.get_task(task_id)
        if existing is not None:
            path = existing.get("input_path") or self.storage.input_path(task_id)
            return IngressResult(True, existing.get("state", "queued"), task_id,
                                 str(path) if path else None)

        task_id = None
        try:
            task_id, _staged_metadata = self.storage.stage_input(
                source_path,
                source_chat=str(source_chat) if source_chat is not None else None,
                source_message_id=(
                    str(source_message) if source_message is not None else None
                ),
                idempotency_key=key,
            )
            staged_path = self.storage.input_path(task_id)
            if staged_path is None:
                raise RuntimeError("staged image path is missing")
            quality = self.quality_gate.assess(staged_path)
            if not self.storage.update_metadata(task_id, {
                "quality": quality.as_dict(),
                "caption": event.get("caption"),
                "media_group_id": event.get("media_group_id"),
                "source_kind": event.get("kind"),
            }):
                raise RuntimeError("could not persist image quality metadata")
            if quality.status == "reject":
                self._reject_staged_task(task_id, "; ".join(quality.reasons))
                return IngressResult(False, "rejected", task_id, str(staged_path),
                                     quality.as_dict(), "; ".join(quality.reasons))

            payload = {
                "image_path": str(staged_path),
                "source": {
                    "chat_id": source_chat,
                    "message_id": source_message,
                    "user_id": event.get("user_id"),
                    "caption": event.get("caption"),
                    "media_group_id": event.get("media_group_id"),
                },
                "quality": quality.as_dict(),
                "schema": schema,
                "requires_review": requires_review,
            }
            if event.get("prompt"):
                payload["prompt"] = event["prompt"]
            added = self.scheduler.add_task(
                task_id,
                "vision",
                payload,
                mode="vision",
                schedule_type="nightly",
                priority=50,
                idempotency_key=key,
                input_path=str(staged_path),
            )
            if not added:
                self._reject_staged_task(task_id, "scheduler rejected duplicate task")
                return IngressResult(False, "rejected", task_id, str(staged_path),
                                     quality.as_dict(), "scheduler rejected task")
            if not self.storage.update_metadata(task_id, {"status": "queued"}):
                raise RuntimeError("could not mark staged image queued")
            return IngressResult(
                True, "queued", task_id, str(staged_path), quality.as_dict()
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._reject_staged_task(task_id, str(exc))
            staged_path = self.storage.input_path(task_id) if task_id else None
            return IngressResult(
                False, "rejected", task_id=task_id,
                path=str(staged_path) if staged_path else None, error=str(exc)
            )


def main(argv: list[str] | None = None) -> int:
    """Read one Hermes event from stdin and emit one JSON result."""
    if argv is None:
        argv = sys.argv[1:]
    if argv != ["--json-stdin"]:
        print("usage: gtx-image-ingress --json-stdin", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError("event must be a JSON object")
        result = IngressResult(
            accepted=False,
            status="rejected",
            error=TelegramImageIngress.DISABLED_REASON,
        )
        print(json.dumps(asdict(result), sort_keys=True))
        return 2
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"accepted": False, "status": "rejected", "error": str(exc)}))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
