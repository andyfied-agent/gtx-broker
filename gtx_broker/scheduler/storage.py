"""Storage contract for task file handling.

Implements the storage contract from Vision Scheduler Architecture:
- Accepted image formats and MIME validation
- Size limits and safe filename handling
- Task ID and idempotency key generation
- Atomic file publication before task becomes visible
- Path traversal prevention and symlink refusal
"""

from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import hashlib
import json
import os
import magic  # python-magic for MIME type detection


class StorageContract:
    """Validates and publishes task input files according to storage contract."""

    # Configuration
    MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MiB
    SUPPORTED_FORMATS = {
        "image/jpeg": ["jpg", "jpeg"],
        "image/png": ["png"],
        "image/webp": ["webp"],
        "image/gif": ["gif"],
        "image/bmp": ["bmp"],
    }
    SUPPORTED_EXTENSIONS = set()
    for exts in SUPPORTED_FORMATS.values():
        SUPPORTED_EXTENSIONS.update(exts)

    def __init__(self, base_path: Path):
        """Initialize storage contract.

        Args:
            base_path: Base directory for storage (e.g., /mnt/scratch/gtx-images)
        """
        self.base_path = Path(base_path)
        self.incoming_path = self.base_path / "incoming"
        self.processing_path = self.base_path / "processing"
        self.processed_path = self.base_path / "processed"
        self.tmp_path = self.base_path / "tmp"
        self.metadata_path = self.base_path / "metadata"

        # Ensure directories exist
        for path in [self.incoming_path, self.processing_path,
                     self.processed_path, self.tmp_path, self.metadata_path]:
            path.mkdir(parents=True, exist_ok=True)

    def validate_input(self, file_path: Path) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Validate an input file against storage contract.

        Args:
            file_path: Path to the file to validate

        Returns:
            Tuple of (is_valid, error_message, metadata)
            If valid, metadata contains validated file info
        """
        # Check file exists
        if not file_path.exists():
            return False, "File does not exist", None

        # Check not a symlink (path traversal prevention)
        if file_path.is_symlink():
            return False, "Symlinks are not allowed", None

        # Check file size
        try:
            file_size = file_path.stat().st_size
            if file_size > self.MAX_FILE_SIZE:
                return False, f"File size {file_size} exceeds limit {self.MAX_FILE_SIZE}", None
        except OSError as e:
            return False, f"Cannot read file: {e}", None

        # Check file extension
        ext = file_path.suffix.lower().lstrip(".")
        if ext not in self.SUPPORTED_EXTENSIONS:
            return False, f"Unsupported format: .{ext}", None

        # Check MIME type
        try:
            mime = magic.Magic(mime=True).from_file(str(file_path))
            if mime not in self.SUPPORTED_FORMATS:
                return False, f"Unsupported MIME type: {mime}", None
        except Exception as e:
            return False, f"MIME detection failed: {e}", None

        # Verify extension matches MIME type
        expected_mimes = self.SUPPORTED_FORMATS.get(mime, [])
        if ext not in expected_mimes:
            return False, f"MIME type {mime} mismatch with extension .{ext}", None

        # Generate metadata
        try:
            with open(file_path, "rb") as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            return False, f"Cannot read file for hash: {e}", None

        metadata = {
            "filename": file_path.name,
            "extension": ext,
            "mime_type": mime,
            "file_size": file_size,
            "content_hash": content_hash,
            "validated_at": datetime.utcnow().isoformat(),
        }

        return True, "", metadata

    def generate_task_id(self, source_chat: Optional[str] = None,
                         source_message_id: Optional[str] = None,
                         idempotency_key: Optional[str] = None) -> str:
        """Generate a unique task ID.

        Args:
            source_chat: Telegram chat ID (if applicable)
            source_message_id: Telegram message ID (if applicable)
            idempotency_key: Pre-computed idempotency key

        Returns:
            Task ID string (e.g., "task-a1b2c3d4")
        """
        if idempotency_key:
            # Use pre-computed key for idempotency
            return f"task-{idempotency_key[:8]}"

        # Generate from timestamp + random
        import uuid
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        unique = uuid.uuid4().hex[:8]
        return f"task-{timestamp}-{unique}"

    def generate_idempotency_key(self, source_chat: str, source_message_id: str,
                                  content_hash: str) -> str:
        """Generate an idempotency key from Telegram source and content.

        Args:
            source_chat: Telegram chat ID
            source_message_id: Telegram message ID
            content_hash: SHA256 hash of file content

        Returns:
            Idempotency key string
        """
        key_string = f"{source_chat}:{source_message_id}:{content_hash}"
        return hashlib.sha256(key_string.encode()).hexdigest()

    def stage_input(self, source_path: Path, source_chat: Optional[str] = None,
                    source_message_id: Optional[str] = None,
                    idempotency_key: Optional[str] = None) -> tuple[str, Dict[str, Any]]:
        """Atomically stage an input file into incoming directory.

        Creates task-<id>/image.<ext> with metadata.json.

        Args:
            source_path: Path to source file (validated)
            source_chat: Telegram chat ID
            source_message_id: Telegram message ID
            idempotency_key: Idempotency key from source

        Returns:
            Tuple of (task_id, metadata)
        """
        # Validate first
        is_valid, error, metadata = self.validate_input(source_path)
        if not is_valid:
            raise ValueError(f"Input validation failed: {error}")

        # Generate task ID
        task_id = self.generate_task_id(
            source_chat=source_chat,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
        )

        # Create task directory
        task_dir = self.incoming_path / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Stage file atomically (write to tmp first, then move)
        temp_file = self.tmp_path / f"{task_id}.tmp"
        target_file = task_dir / f"image.{metadata['extension']}"

        try:
            # Copy to temp location
            import shutil
            shutil.copy2(str(source_path), str(temp_file))

            # Atomic move to final location
            os.rename(str(temp_file), str(target_file))

            # Create metadata.json
            metadata_task = {
                "task_id": task_id,
                "source_chat": source_chat,
                "source_message_id": source_message_id,
                "idempotency_key": idempotency_key,
                "content_hash": metadata["content_hash"],
                "mime_type": metadata["mime_type"],
                "file_size": metadata["file_size"],
                "status": "accepted",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            metadata_file = task_dir / "metadata.json"
            temp_metadata = self.tmp_path / f"{task_id}.meta.json"

            with open(temp_metadata, "w") as f:
                json.dump(metadata_task, f, indent=2)

            os.rename(str(temp_metadata), str(metadata_file))

            return task_id, metadata_task

        except Exception as e:
            # Clean up on failure
            if temp_file.exists():
                temp_file.unlink()
            if target_file.exists():
                target_file.unlink()
            if temp_metadata.exists():
                temp_metadata.unlink()
            if task_dir.exists() and not task_dir.iterdir():
                task_dir.rmdir()
            raise ValueError(f"Staging failed: {e}")

    def claim_for_processing(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a task from incoming to processing.

        Args:
            task_id: Task ID to claim

        Returns:
            Task metadata if claimed, None if not found or already claimed
        """
        incoming_task_dir = self.incoming_path / task_id
        processing_task_dir = self.processing_path / task_id

        if not incoming_task_dir.exists():
            return None

        try:
            # Read metadata
            metadata_file = incoming_task_dir / "metadata.json"
            with open(metadata_file, "r") as f:
                metadata = json.load(f)

            if metadata.get("status") != "accepted":
                return None

            # Move to processing
            import shutil
            shutil.move(str(incoming_task_dir), str(processing_task_dir))

            # Update metadata
            metadata["status"] = "processing"
            metadata["claimed_at"] = datetime.utcnow().isoformat()
            metadata_file = processing_task_dir / "metadata.json"

            with open(metadata_file, "w") as f:
                json.dump(metadata, f, indent=2)

            return metadata

        except Exception as e:
            raise ValueError(f"Claim failed: {e}")

    def complete_task(self, task_id: str, output_path: Optional[Path] = None,
                      result: Optional[Dict[str, Any]] = None) -> bool:
        """Move task from processing to processed directory.

        Args:
            task_id: Task ID to complete
            output_path: Optional output file path
            result: Optional result metadata

        Returns:
            True if successful
        """
        processing_task_dir = self.processing_path / task_id

        if not processing_task_dir.exists():
            return False

        try:
            # Read current metadata
            metadata_file = processing_task_dir / "metadata.json"
            with open(metadata_file, "r") as f:
                metadata = json.load(f)

            # Move to processed
            import shutil
            processed_task_dir = self.processed_path / task_id

            # If output_path provided, move it to processed
            if output_path and output_path.exists():
                shutil.copy2(str(output_path), str(processed_task_dir))

            shutil.move(str(processing_task_dir), str(processed_task_dir))

            # Update metadata
            metadata["status"] = "processed"
            metadata["completed_at"] = datetime.utcnow().isoformat()
            if result:
                metadata["result"] = result
            metadata_file = processed_task_dir / "metadata.json"

            with open(metadata_file, "w") as f:
                json.dump(metadata, f, indent=2)

            return True

        except Exception as e:
            raise ValueError(f"Task completion failed: {e}")

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task metadata from any state directory.

        Args:
            task_id: Task ID

        Returns:
            Task metadata if found, None otherwise
        """
        for base_dir in [self.incoming_path, self.processing_path, self.processed_path]:
            task_dir = base_dir / task_id
            if task_dir.exists():
                metadata_file = task_dir / "metadata.json"
                if metadata_file.exists():
                    with open(metadata_file, "r") as f:
                        return json.load(f)
        return None

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """Clean up tasks older than specified days.

        Args:
            days: Number of days to retain

        Returns:
            Number of tasks removed
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=days)
        removed = 0

        for base_dir in [self.processed_path]:  # Only clean processed
            if not base_dir.exists():
                continue

            for task_dir in base_dir.iterdir():
                if not task_dir.is_dir():
                    continue

                metadata_file = task_dir / "metadata.json"
                if not metadata_file.exists():
                    continue

                try:
                    with open(metadata_file, "r") as f:
                        metadata = json.load(f)

                    completed_at = datetime.fromisoformat(metadata.get("completed_at", ""))
                    if completed_at < cutoff:
                        import shutil
                        shutil.rmtree(str(task_dir))
                        removed += 1

                except Exception:
                    # Skip malformed metadata
                    continue

        return removed
