"""Handler interfaces for task processing.

Defines the handler contract that vision.py and coding.py will implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
from enum import Enum
import base64
import hashlib
import json
import logging
import math
import mimetypes
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError


logger = logging.getLogger(__name__)


class HandlerResult(Enum):
    """Result of handler execution."""
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"
    AWAITING_REVIEW = "awaiting_review"
    WORKER_UNAVAILABLE = "worker_unavailable"


@dataclass
class HandlerAttempt:
    """Record of a handler attempt."""
    attempt_number: int
    worker_profile: str
    model_profile: Optional[str]
    start_at: str
    end_at: Optional[str]
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    failure_class: Optional[str]
    tokens_used: Optional[int] = None


class TaskHandler(ABC):
    """Abstract base class for task handlers.

    Each handler type (vision, coding, data, maintenance) implements this interface.
    """

    @property
    @abstractmethod
    def handler_type(self) -> str:
        """Return the handler type (e.g., 'vision', 'coding')."""
        pass

    @abstractmethod
    def can_handle(self, task_payload: Dict[str, Any]) -> bool:
        """Check if this handler can process the task.

        Args:
            task_payload: Task payload from database

        Returns:
            True if handler can process this task
        """
        pass

    @abstractmethod
    def execute(self, task: Dict[str, Any]) -> HandlerResult:
        """Execute the handler on the task.

        Args:
            task: Task dict from scheduler (has 'kind', 'payload', etc.)

        Returns:
            HandlerResult indicating outcome
        """
        pass

    @abstractmethod
    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate handler output.

        Args:
            output: Handler output data

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass


class VisionHandler(TaskHandler):
    """Vision task handler (implements TaskHandler).

    Processes images using P40 vision worker with approved projector.
    """

    SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
    RECEIPT_SCHEMA = "receipt"
    IMAGE_DESCRIPTION_SCHEMA = "image_description"

    RECEIPT_PROMPT = """Extract this receipt image into JSON with exactly these keys:
merchant, date, currency, totals, line_items.
Use totals as an object with subtotal, vat, total, and savings numeric values or null.
Use line_items as an array of objects with description, quantity, and price.
Do not infer quantities. Treat discounts and savings as line items only when visibly
shown, with negative prices when appropriate. Return JSON only."""
    IMAGE_DESCRIPTION_PROMPT = """Describe this image as JSON with exactly these keys:
description, objects, text, confidence.
Use description for a concise literal description. Use objects as an array of objects
with label and attributes. Use text as an array of text strings that are visibly
present. Use confidence as a number from 0 to 1, or null when uncertain. Do not
invent details and return JSON only."""
    DEFAULT_PROMPT = RECEIPT_PROMPT
    SUPPORTED_SCHEMAS = {RECEIPT_SCHEMA, IMAGE_DESCRIPTION_SCHEMA}

    def __init__(self, endpoint: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None, max_image_bytes: int = 25 * 1024 * 1024):
        self.endpoint = (endpoint or os.getenv("P40_VISION_ENDPOINT", "http://127.0.0.1:11436/v1")).rstrip("/")
        self.model = model or os.getenv("P40_VISION_MODEL", "Qwen3.5-35B-A3B-UD-Q2_K_XL.gguf")
        self.timeout = timeout or float(os.getenv("P40_VISION_TIMEOUT", "120"))
        self.max_image_bytes = max_image_bytes
        self.last_result: Optional[Dict[str, Any]] = None

    @property
    def handler_type(self) -> str:
        return "vision"

    def can_handle(self, task: Dict[str, Any]) -> bool:
        """Check if this handler can process the task.

        Args:
            task: Task dict from scheduler (has 'kind' key)

        Returns:
            True if handler can process this task
        """
        return task.get("kind") == "vision"

    def execute(self, task: Dict[str, Any]) -> HandlerResult:
        """Execute vision task.

        Args:
            task: Task dict from scheduler (has 'kind', 'payload', 'input_path', etc.)

        Returns:
            HandlerResult indicating success, failure, retry, or worker_unavailable
        """
        self.last_result = None
        image_path = self._image_path(task)
        if image_path is None:
            logger.error("Vision task %s has no usable image path", task.get("id"))
            return HandlerResult.FAILED
        valid, error = self._validate_image(image_path)
        if not valid:
            logger.error("Vision task %s rejected: %s", task.get("id"), error)
            return HandlerResult.FAILED
        payload = task.get("payload") or {}
        schema = self._schema_for_payload(payload)
        if schema is None:
            logger.error("Vision task %s requested an unsupported output schema", task.get("id"))
            return HandlerResult.FAILED
        if not self._model_available():
            logger.warning("Vision model unavailable at %s", self.endpoint)
            return HandlerResult.WORKER_UNAVAILABLE

        prompt = payload.get("prompt") or self._prompt_for_schema(schema)
        try:
            raw = self._call_endpoint(image_path, prompt)
            result = self._parse_output(raw)
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            logger.warning("Vision endpoint request failed: %s", exc)
            return HandlerResult.RETRY
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("Vision output was invalid: %s", exc)
            return HandlerResult.FAILED

        valid, error = self.validate_output(result, schema=schema)
        if not valid:
            logger.error("Vision output failed validation: %s", error)
            return HandlerResult.FAILED
        self.last_result = result
        if payload.get("requires_review") is True:
            return HandlerResult.AWAITING_REVIEW
        return HandlerResult.SUCCESS

    @staticmethod
    def _image_path(task: Dict[str, Any]) -> Optional[Path]:
        payload = task.get("payload") or {}
        candidate = task.get("input_path") or payload.get("input_path") or payload.get("image_path")
        if not candidate:
            return None
        path = Path(str(candidate)).expanduser()
        if path.is_dir():
            images = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in {
                ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"
            })
            return images[0] if images else None
        return path

    def _validate_image(self, image_path: Path) -> tuple[bool, str]:
        if not image_path.exists() or not image_path.is_file():
            return False, "image does not exist"
        if image_path.is_symlink():
            return False, "symlink images are not allowed"
        if image_path.stat().st_size > self.max_image_bytes:
            return False, "image exceeds configured size limit"
        mime, _ = mimetypes.guess_type(image_path.name)
        if mime not in self.SUPPORTED_MIME_TYPES:
            return False, f"unsupported image type: {mime or 'unknown'}"
        try:
            with Image.open(image_path) as image:
                image.verify()
        except (OSError, UnidentifiedImageError):
            return False, "image content is unreadable"
        return True, ""

    @classmethod
    def _schema_for_payload(cls, payload: Dict[str, Any]) -> Optional[str]:
        schema = payload.get("schema", payload.get("output_schema", cls.RECEIPT_SCHEMA))
        if not isinstance(schema, str) or schema not in cls.SUPPORTED_SCHEMAS:
            return None
        return schema

    @classmethod
    def _prompt_for_schema(cls, schema: str) -> str:
        if schema == cls.IMAGE_DESCRIPTION_SCHEMA:
            return cls.IMAGE_DESCRIPTION_PROMPT
        return cls.RECEIPT_PROMPT

    def _model_available(self) -> bool:
        request = Request(f"{self.endpoint}/models", method="GET")
        try:
            with urlopen(request, timeout=min(self.timeout, 10)) as response:
                data = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False
        models = data.get("data", []) if isinstance(data, dict) else []
        configured_names = {self.model, Path(self.model).name}
        for item in models:
            if not isinstance(item, dict):
                continue
            advertised = [item.get("id"), item.get("name")]
            advertised.extend(item.get("aliases", []))
            if any(
                isinstance(name, str)
                and (name in configured_names or Path(name).name in configured_names)
                for name in advertised
            ):
                return True
        return False

    def _call_endpoint(self, image_path: Path, prompt: str) -> str:
        mime, _ = mimetypes.guess_type(image_path.name)
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]}],
            "temperature": 0,
            "max_tokens": 1200,
        }
        request = Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty vision response")
        return content.strip()

    @staticmethod
    def _parse_output(raw: str) -> Dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("response is not JSON")
            value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise ValueError("vision response must be an object")
        return value

    def validate_output(
        self, output: Dict[str, Any], *, schema: str = RECEIPT_SCHEMA
    ) -> tuple[bool, Optional[str]]:
        """Validate vision output.

        Args:
            output: Vision extraction output

        Returns:
            Tuple of (is_valid, error_message)
        """
        if schema == self.IMAGE_DESCRIPTION_SCHEMA:
            return self._validate_image_description(output)
        if schema != self.RECEIPT_SCHEMA:
            return False, f"unsupported output schema: {schema}"

        required = {"merchant", "date", "currency", "totals", "line_items"}
        missing = required - output.keys()
        if missing:
            return False, f"missing fields: {sorted(missing)}"
        if not isinstance(output["merchant"], str) or not output["merchant"].strip():
            return False, "merchant must be a non-empty string"
        if not isinstance(output["date"], (str, type(None))):
            return False, "date must be a string or null"
        if not isinstance(output["currency"], (str, type(None))) or (
                output["currency"] is not None and len(output["currency"]) != 3):
            return False, "currency must be a three-letter code or null"
        totals = output["totals"]
        if not isinstance(totals, dict):
            return False, "totals must be an object"
        for name in ("subtotal", "vat", "total", "savings"):
            value = totals.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                return False, f"totals.{name} must be numeric or null"
        line_items = output["line_items"]
        if not isinstance(line_items, list):
            return False, "line_items must be an array"
        for index, item in enumerate(line_items):
            if not isinstance(item, dict) or not isinstance(item.get("description"), str):
                return False, f"line_items[{index}] has no description"
            for name in ("quantity", "price"):
                value = item.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    return False, f"line_items[{index}].{name} must be numeric or null"
        return True, None

    @staticmethod
    def _validate_image_description(output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        required = {"description", "objects", "text", "confidence"}
        missing = required - output.keys()
        if missing:
            return False, f"missing fields: {sorted(missing)}"
        if not isinstance(output["description"], str) or not output["description"].strip():
            return False, "description must be a non-empty string"
        for field in ("objects", "text"):
            if not isinstance(output[field], list):
                return False, f"{field} must be an array"
        for index, item in enumerate(output["objects"]):
            if not isinstance(item, dict) or not isinstance(item.get("label"), str):
                return False, f"objects[{index}] needs a label"
            if not item["label"].strip():
                return False, f"objects[{index}].label must be non-empty"
            if "attributes" in item and not isinstance(item["attributes"], dict):
                return False, f"objects[{index}].attributes must be an object"
        if any(not isinstance(item, str) for item in output["text"]):
            return False, "text entries must be strings"
        confidence = output["confidence"]
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            return False, "confidence must be a number from 0 to 1 or null"
        return True, None


class CodingHandler(TaskHandler):
    """Coding task handler (implements TaskHandler).

    Processes coding tasks using the selected coding worker. Immediate tasks
    default to P40; the scheduler marks nightly/batch tasks as slow-coder work.
    """

    def __init__(self, timeout: Optional[float] = None):
        self.timeout = timeout
        self.last_result: Optional[Dict[str, Any]] = None

    @property
    def handler_type(self) -> str:
        return "coding"

    def can_handle(self, task: Dict[str, Any]) -> bool:
        """Check if this handler can process the task.

        Args:
            task: Task dict from scheduler (has 'kind' key)

        Returns:
            True if handler can process this task
        """
        return task.get("kind") == "coding"

    def execute(self, task: Dict[str, Any]) -> HandlerResult:
        """Execute coding task.

        Args:
            task: Task dict from scheduler (has 'kind', 'payload', 'goal', etc.)

        Returns:
            HandlerResult indicating success, failure, retry, or worker_unavailable
        """
        self.last_result = None
        payload = task.get("payload") or {}
        worker_profile = task.get("worker_profile") or payload.get("worker_profile") or "p40-coding"
        worktree = payload.get("worktree_path") or payload.get("repository_path")
        command_env = "SLOW_CODER_COMMAND" if worker_profile == "slow-coder" else "P40_CODING_COMMAND"
        command = payload.get("executor_command") or os.getenv(command_env)
        if not worktree or not command:
            return HandlerResult.WORKER_UNAVAILABLE
        worktree_path = Path(str(worktree)).expanduser()
        if not worktree_path.is_dir():
            return HandlerResult.FAILED
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            return HandlerResult.WORKER_UNAVAILABLE
        instruction = payload.get("instruction") or payload.get("goal") or "Implement the task."
        payload_timeout = payload.get("timeout", payload.get("timeout_seconds"))
        if payload_timeout is not None:
            timeout = float(payload_timeout)
        elif self.timeout is not None:
            timeout = float(self.timeout)
        else:
            timeout = float(os.getenv(
                "SLOW_CODER_TIMEOUT" if worker_profile == "slow-coder" else "P40_CODING_TIMEOUT",
                "3600" if worker_profile == "slow-coder" else "1800",
            ))
        try:
            before = self._git_status(worktree_path)
            track_commits = payload.get("require_commit", True)
            before_commit = self._git_head(worktree_path) if track_commits else ""
            completed = subprocess.run(
                argv, cwd=worktree_path, input=str(instruction), capture_output=True,
                text=True, timeout=timeout, check=False,
            )
            tests_passed = True
            test_output = ""
            test_command = payload.get("test_command")
            if completed.returncode == 0 and test_command:
                test_argv = shlex.split(test_command) if isinstance(test_command, str) else list(test_command)
                tested = subprocess.run(
                    test_argv, cwd=worktree_path, capture_output=True, text=True,
                    timeout=timeout, check=False,
                )
                tests_passed = tested.returncode == 0
                test_output = (tested.stdout + tested.stderr)[-12000:]
            after = self._git_status(worktree_path)
            after_commit = self._git_head(worktree_path) if track_commits else ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_result = {"error": str(exc), "status": "executor_error"}
            return HandlerResult.RETRY

        changed = sorted(set(after) - set(before))
        committed_files = []
        if before_commit != after_commit:
            committed_files = self._git_changed_files(
                worktree_path, before_commit, after_commit
            )
            changed = sorted(set(changed) | set(committed_files))
        self.last_result = {
            "status": "success" if completed.returncode == 0 and tests_passed else "failed",
            "exit_code": completed.returncode,
            "changed_files": changed,
            "committed_files": committed_files,
            "before_commit": before_commit,
            "after_commit": after_commit,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-12000:],
            "tests_passed": tests_passed,
            "test_output": test_output,
        }
        if completed.returncode != 0 or not tests_passed:
            return HandlerResult.FAILED
        if payload.get("require_commit", True) and self._git_status(worktree_path):
            self.last_result["status"] = "failed_dirty_worktree"
            return HandlerResult.FAILED
        if not changed and not payload.get("allow_no_change", False):
            self.last_result["status"] = "failed_no_change"
            return HandlerResult.FAILED
        # Manifest tasks are deliberately not terminally successful here.
        # Their contract requires Air Review, Codex final review, and PR
        # approval before production activation. The daemon persists this
        # result as awaiting_review so an implementation cannot bypass those
        # gates merely by exiting successfully.
        manifest = payload.get("manifest")
        if isinstance(manifest, dict) and manifest.get("review_policy"):
            self.last_result["status"] = "awaiting_review"
            self.last_result["review_policy"] = dict(manifest["review_policy"])
            return HandlerResult.AWAITING_REVIEW
        return HandlerResult.SUCCESS

    @staticmethod
    def _git_status(worktree: Path) -> list[str]:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree, capture_output=True, text=True, check=True,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _git_head(worktree: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree, capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return ""

    @staticmethod
    def _git_changed_files(worktree: Path, before: str, after: str) -> list[str]:
        revision = f"{before}..{after}" if before else after
        command = ["git", "diff", "--name-only", revision] if before else [
            "git", "show", "--format=", "--name-only", after,
        ]
        result = subprocess.run(
            command,
            cwd=worktree, capture_output=True, text=True, check=True,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate coding output.

        Args:
            output: Coding result with changes, test results, etc.

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not isinstance(output, dict):
            return False, "coding output must be an object"
        return (True, None) if output.get("status") == "success" else (False, "coding task did not succeed")


class ReviewHandler(TaskHandler):
    """Read-only Codex reviewer boundary with Air Review failover.

    The reviewer receives a task manifest on stdin and must return JSON. Any
    worktree mutation is treated as a failed review and is never silently
    retained.
    """

    def __init__(self, timeout: Optional[float] = None):
        self.timeout = timeout or float(os.getenv("AIR_REVIEW_TIMEOUT", "900"))
        self.last_result: Optional[Dict[str, Any]] = None

    @property
    def handler_type(self) -> str:
        return "review"

    def can_handle(self, task: Dict[str, Any]) -> bool:
        return task.get("kind") == "review" or bool(task.get("review_tag") or task.get("review_worker"))

    def _commands(self, task: Dict[str, Any]) -> list[tuple[str, Any]]:
        payload = task.get("payload") or {}
        selected = task.get("worker_profile") or task.get("review_worker")
        if selected == "air-review":
            return [("air-review", payload.get("air_review_command") or
                     payload.get("review_command") or os.getenv("AIR_REVIEW_COMMAND"))]
        return [
            ("codex-review", payload.get("codex_review_command") or
             payload.get("review_command") or os.getenv("CODEX_REVIEW_COMMAND") or
             os.getenv("CODEX_COMMAND")),
            ("air-review", payload.get("air_review_command") or
             os.getenv("AIR_REVIEW_COMMAND")),
        ]

    @staticmethod
    def _git_snapshot(worktree_path: Path) -> Optional[tuple[str, str]]:
        """Return HEAD and a fingerprint of all worktree content changes.

        Git status alone cannot detect a reviewer that commits its changes, so
        the snapshot includes both the current HEAD and the actual tracked
        diff/untracked-file bytes.
        """
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=worktree_path,
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD"], cwd=worktree_path,
                check=True, capture_output=True,
            ).stdout
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=worktree_path, check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            digest = hashlib.sha256(head.encode() + b"\0" + diff + b"\0")
            for relative in sorted(untracked):
                path = worktree_path / relative
                if path.is_file():
                    digest.update(relative.encode() + b"\0" + path.read_bytes())
            return head, digest.hexdigest()
        except (OSError, subprocess.CalledProcessError):
            return None

    def _execute_command(
        self, task: Dict[str, Any], worktree_path: Path, worker: str, command: Any,
    ) -> tuple[HandlerResult, Optional[Dict[str, Any]]]:
        if not command:
            return HandlerResult.WORKER_UNAVAILABLE, {
                "reviewer": worker, "status": "unavailable",
                "failure_kind": "review_unavailable",
                "evidence": [f"{worker} command is not configured"],
            }
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            return HandlerResult.WORKER_UNAVAILABLE, {
                "reviewer": worker, "status": "unavailable",
                "failure_kind": "review_unavailable",
                "evidence": [f"{worker} command is empty"],
            }
        schema_path: Optional[Path] = None
        command_input = json.dumps(task)
        if worker == "codex-review" and Path(argv[0]).name == "codex":
            schema_file = tempfile.NamedTemporaryFile(
                mode="w", suffix="-codex-review.schema.json", delete=False,
            )
            json.dump({
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["passed", "findings", "evidence"],
                "additionalProperties": False,
            }, schema_file)
            schema_file.close()
            schema_path = Path(schema_file.name)
            if "exec" not in argv[1:]:
                argv.insert(1, "exec")
            argv.extend([
                "--ephemeral",
                "--cd", str(worktree_path),
                "--output-schema", str(schema_path),
            ])
            command_input = json.dumps({
                **task,
                "instruction": (
                    "Inspect the current worktree without making changes. "
                    "Return only JSON matching the supplied output schema. "
                    "Do not run pytest or other commands that require a "
                    "writable temporary directory. Only inspect files under "
                    "the current worktree; do not search parent directories."
                ),
            })
        try:
            before = self._git_snapshot(worktree_path)
            if before is None:
                return HandlerResult.FAILED, {
                    "reviewer": worker, "status": "rejected",
                    "failure_kind": "review_mutation_check_failed",
                    "passed": False, "findings": [],
                    "evidence": ["Unable to fingerprint the worktree before review"],
                }
            completed = subprocess.run(
                argv, cwd=worktree_path, input=command_input, capture_output=True,
                text=True, timeout=self.timeout, check=False,
            )
            after = self._git_snapshot(worktree_path)
            if after is None or before != after:
                return HandlerResult.FAILED, {
                    "reviewer": worker, "status": "rejected",
                    "failure_kind": "review_mutated",
                    "passed": False, "findings": [],
                    "evidence": [
                        f"{worker} review modified the worktree; reviewer execution is read-only",
                    ],
                }
            if completed.returncode != 0:
                logger.warning("%s reviewer exited with status %s", worker, completed.returncode)
                return HandlerResult.WORKER_UNAVAILABLE, {
                    "reviewer": worker, "status": "unavailable",
                    "failure_kind": "review_unavailable",
                    "exit_code": completed.returncode,
                    "error": completed.stderr[-1000:],
                    "evidence": [f"{worker} reviewer exited with status {completed.returncode}"],
                }
            result = VisionHandler._parse_output(completed.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            after = self._git_snapshot(worktree_path)
            if before is not None and (after is None or before != after):
                return HandlerResult.FAILED, {
                    "reviewer": worker, "status": "rejected",
                    "failure_kind": "review_mutated",
                    "passed": False, "findings": [],
                    "evidence": [
                        f"{worker} review modified the worktree; reviewer execution is read-only",
                    ],
                }
            logger.warning("%s reviewer unavailable: %s", worker, exc)
            return HandlerResult.WORKER_UNAVAILABLE, {
                "reviewer": worker, "status": "unavailable",
                "failure_kind": "review_unavailable", "error": str(exc),
                "timeout": isinstance(exc, subprocess.TimeoutExpired),
                "evidence": [f"{worker} reviewer unavailable: {exc}"],
            }
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("%s review output was invalid: %s", worker, exc)
            return HandlerResult.FAILED, {
                "reviewer": worker, "status": "rejected",
                "failure_kind": "invalid_review_output", "passed": False,
                "findings": [], "error": str(exc),
                "evidence": [f"{worker} review output was invalid: {exc}"],
            }
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)
        valid, error = self.validate_output(result)
        if not valid:
            logger.error("%s review output failed validation: %s", worker, error)
            result = dict(result) if isinstance(result, dict) else {}
            result.update({
                "reviewer": worker, "status": "rejected",
                "failure_kind": "invalid_review_output", "passed": False,
                "error": error,
                "evidence": [f"{worker} review output failed validation: {error}"],
            })
            return HandlerResult.FAILED, result
        result = dict(result)
        result["reviewer"] = worker
        result.setdefault("evidence", [])
        result["status"] = "approved" if result["passed"] else "rejected"
        result["failure_kind"] = "approved" if result["passed"] else "rejected_code"
        return (HandlerResult.SUCCESS if result["passed"] else HandlerResult.FAILED), result

    def execute(self, task: Dict[str, Any]) -> HandlerResult:
        self.last_result = None
        payload = task.get("payload") or {}
        worktree = payload.get("worktree_path") or payload.get("repository_path")
        if not worktree:
            return HandlerResult.WORKER_UNAVAILABLE
        worktree_path = Path(str(worktree)).expanduser()
        if not worktree_path.is_dir():
            return HandlerResult.FAILED
        attempts: list[Dict[str, Any]] = []
        for worker, command in self._commands(task):
            result, output = self._execute_command(task, worktree_path, worker, command)
            if output is not None:
                attempts.append(output)
            if result in {HandlerResult.SUCCESS, HandlerResult.FAILED}:
                self.last_result = dict(output or {})
                self.last_result["review_attempts"] = attempts
                self.last_result["fallback_used"] = len(attempts) > 1
                return result
            if result != HandlerResult.WORKER_UNAVAILABLE:
                return result
        self.last_result = {
            "status": "unavailable", "failure_kind": "review_unavailable",
            "review_attempts": attempts, "fallback_used": len(attempts) > 1,
        }
        return HandlerResult.WORKER_UNAVAILABLE

    def validate_output(self, output: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        if not isinstance(output, dict):
            return False, "review output must be an object"
        if not isinstance(output.get("passed"), bool):
            return False, "review output passed must be boolean"
        if not isinstance(output.get("findings"), list):
            return False, "review output findings must be an array"
        for finding in output["findings"]:
            if isinstance(finding, str):
                continue
            if not isinstance(finding, dict) or not finding.get("id") or not finding.get("severity"):
                return False, "each finding needs id and severity"
        return True, None


# Handler factory
HANDLERS = [VisionHandler(), CodingHandler(), ReviewHandler()]


def initialize_handlers() -> Dict[str, TaskHandler]:
    """Initialize all handlers.
    
    Returns:
        Dict mapping task kind to handler instance
    """
    handlers = {}
    for handler in HANDLERS:
        handlers[handler.handler_type] = handler
    return handlers


def get_handler_for_task(task: Dict[str, Any]) -> Optional[TaskHandler]:
    """Find handler for a task.
    
    Args:
        task: Task dict from scheduler (has 'kind' key)
    
    Returns:
        Matching TaskHandler or None
    """
    if task.get("review_tag") or task.get("review_worker"):
        return next(handler for handler in HANDLERS if handler.handler_type == "review")
    task_kind = task.get("kind")
    if not task_kind:
        return None
    
    for handler in HANDLERS:
        if handler.handler_type == task_kind:
            return handler
    return None
