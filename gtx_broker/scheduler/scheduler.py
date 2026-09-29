"""Core scheduler implementation with SQLite persistence.

Replaces JSON-based scheduler with SQLite implementation matching the
scheduler architecture specification.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import sqlite3
from pathlib import Path
import sys
import logging

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from gtx_broker.scheduler.storage import StorageContract
from gtx_broker.scheduler.workers import WorkerRegistry, WorkerStatus, initialize_workers
from gtx_broker.scheduler.policies import get_dispatch_policy, ScheduleWindow
from gtx_broker.scheduler.migrations import MigrationRunner


logger = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    """Scheduler configuration."""
    db_path: str = "/mnt/scratch/gtx-images/metadata/tasks.db"
    max_concurrent: int = 1
    poll_interval: float = 5.0


class Scheduler:
    """Task scheduler with SQLite persistence.

    Implements the state machine from the scheduler architecture:
    accepted -> queued -> claimed -> running -> succeeded
                                |          |
                                |          +-> retry_wait -> queued
                                |          +-> awaiting_review
                                |          +-> failed_terminal
                                |          +-> cancelled
                                +-> retry_wait (for failed starts)
    """

    STATE_TRANSITIONS = {
        "accepted": ["queued", "cancelled"],
        "queued": ["claimed", "cancelled"],
        "claimed": ["running", "retry_wait", "cancelled"],
        "running": ["succeeded", "failed_terminal", "retry_wait", "awaiting_review"],
        "succeeded": [],
        "failed_terminal": [],
        "retry_wait": ["queued"],
        "awaiting_review": ["queued", "succeeded", "failed_terminal"],
        "cancelled": [],
    }

    def __init__(self, config: Optional[SchedulerConfig] = None):
        """Initialize scheduler.

        Args:
            config: Scheduler configuration
        """
        self.config = config or SchedulerConfig()
        self.db_path = Path(self.config.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._policy = get_dispatch_policy()
        self._storage = StorageContract(Path(self.config.db_path).parent.parent)
        self._worker_registry = WorkerRegistry(self.db_path)

        # Initialize database schema and run migrations
        self._init_db()
        self._upgrade_columns_if_missing()
        self._migrations = MigrationRunner(self.config.db_path)
        if not self._migrations.run_all():
            import logging
            logging.getLogger(__name__).error("Failed to apply database migrations")
            raise RuntimeError("Database migration failed")

        # Initialize default workers
        initialize_workers(self.db_path)

    def _upgrade_columns_if_missing(self) -> None:
        """Add missing columns to existing tasks table.

        This handles upgrade from old schema where tasks table doesn't have
        review_tag, schedule_type, or batch_epoch_id columns.
        """
        import logging
        logger = logging.getLogger(__name__)

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get current column names
            cursor.execute("PRAGMA table_info(tasks)")
            current_columns = {row[1] for row in cursor.fetchall()}

            # Add missing columns
            new_columns = [
                ("review_tag", "TEXT"),
                ("schedule_type", "TEXT"),
                ("batch_epoch_id", "TEXT"),
            ]

            for col_name, col_type in new_columns:
                if col_name not in current_columns:
                    cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Added column {col_name} to tasks table")

            conn.commit()

        except sqlite3.Error as e:
            logger.error(f"Failed to upgrade tasks table schema: {e}")
        finally:
            if conn is not None:
                conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with WAL mode and longer timeout."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_size_limit=10485760")  # 10MB
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize database schema."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Create tasks table with retry_at column and tagging fields
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'batch',
            payload TEXT,
            input_path TEXT,
            output_path TEXT,
            idempotency_key TEXT UNIQUE,
            deadline_timestamp TEXT,
            retry_policy TEXT,
            retry_at TIMESTAMP,
            error TEXT,
            review_tag INTEGER,
            schedule_type TEXT,
            batch_epoch_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Create task_attempts table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            worker_profile TEXT,
            model_profile TEXT,
            start_at TIMESTAMP,
            end_at TIMESTAMP,
            result TEXT,
            failure_class TEXT,
            resource_evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
        """)

        # Create task_events table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()
        conn.close()

    def _emit_event(self, task_id: str, event_type: str, from_state: Optional[str] = None,
                    to_state: Optional[str] = None, details: Optional[str] = None):
        """Record a task event."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
            INSERT INTO task_events (task_id, event_type, from_state, to_state, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                task_id, event_type, from_state, to_state, details,
                datetime.now(timezone.utc).isoformat(),
            ))

            conn.commit()
            conn.close()
        except sqlite3.OperationalError:
            # If event logging fails, don't break the main operation
            pass

    def _validate_transition(self, task_id: str, from_state: str, to_state: str) -> bool:
        """Validate that a state transition is allowed by STATE_TRANSITIONS table."""
        allowed = self.STATE_TRANSITIONS.get(from_state, [])
        return to_state in allowed

    def add_task(self, task_id: str, kind: str, payload: Dict[str, Any],
                 mode: Optional[str] = None, priority: int = 0,
                 idempotency_key: Optional[str] = None, *,
                 schedule_type: Optional[str] = None,
                 review_tag: bool = False,
                 review_worker: Optional[str] = None,
                 batch_epoch_id: Optional[str] = None,
                 input_path: Optional[str] = None,
                 output_path: Optional[str] = None) -> bool:
        """Add a task to the scheduler.

        Args:
            task_id: Unique task identifier
            kind: Task kind (e.g., "vision", "coding")
            payload: Task payload dictionary
            mode: Task mode ("immediate", "batch", "vision", "maintenance").
                If omitted, vision tasks use ``vision`` and other tasks use
                ``immediate``.
            priority: Task priority (higher = more urgent)
            idempotency_key: Optional idempotency key to prevent duplicates

        Returns:
            True if added, False if duplicate
        """
        valid_modes = {"immediate", "batch", "vision", "maintenance"}
        valid_schedules = {"immediate", "batch", "nightly"}
        mode = mode or ("vision" if kind == "vision" else "immediate")
        if mode not in valid_modes:
            raise ValueError(f"unsupported task mode: {mode}")
        if schedule_type is None:
            if kind == "vision" or mode == "vision":
                schedule_type = "nightly"
            elif mode == "batch":
                schedule_type = "batch"
            else:
                schedule_type = "immediate"
        if schedule_type not in valid_schedules:
            raise ValueError(f"unsupported schedule type: {schedule_type}")
        if review_tag and review_worker is None:
            review_worker = "air-review"

        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Check for duplicate idempotency key
            if idempotency_key:
                cursor.execute("SELECT id FROM tasks WHERE idempotency_key = ?", (idempotency_key,))
                if cursor.fetchone():
                    conn.close()
                    return False

            try:
                cursor.execute("""
                INSERT INTO tasks (
                    id, kind, state, priority, mode, payload, input_path,
                    output_path, idempotency_key, review_tag, schedule_type,
                    review_worker, batch_epoch_id, created_at, updated_at
                )
                VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, kind, priority, mode,
                    self._json_dump(payload), input_path, output_path,
                    idempotency_key, int(review_tag), schedule_type,
                    review_worker, batch_epoch_id,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ))

                self._emit_event(task_id, "task_added", from_state=None, to_state="queued",
                                details=(
                                    f"kind={kind}, mode={mode}, schedule={schedule_type}, "
                                    f"priority={priority}, review_tag={int(review_tag)}"
                                ))

                conn.commit()
                conn.close()
                return True

            except sqlite3.IntegrityError:
                conn.close()
                return False

        except sqlite3.OperationalError:
            return False

    def claim_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a task for processing.

        Args:
            task_id: Task ID to claim

        Returns:
            Task data with updated state if claimed, None if not available
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get current state before update
            cursor.execute("SELECT state, kind FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return None

            from_state = row["state"]

            # Validate transition is legal per STATE_TRANSITIONS
            if from_state != "queued":
                conn.close()
                return None

            if not self._validate_transition(task_id, from_state, "claimed"):
                conn.close()
                return None

            # Atomically claim the specific task (not first available)
            cursor.execute("""
            UPDATE tasks SET state = 'claimed', updated_at = ?
            WHERE id = ? AND state = 'queued'
            """, (datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount == 0:
                conn.close()
                return None

            # Fetch the updated row (now with state='claimed')
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            conn.commit()

            # Emit task_claimed event
            self._emit_event(task_id, "task_claimed", from_state="queued", to_state="claimed")

            conn.close()

            return self._row_to_dict(row) if row else None

        except sqlite3.OperationalError:
            return None

    def _close_current_attempt(self, task_id: str) -> bool:
        """Close any open attempt for a task by setting end_at."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE task_attempts SET end_at = ?
                WHERE task_id = ? AND end_at IS NULL
            """, (datetime.now(timezone.utc).isoformat(), task_id))

            closed = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return closed
        except sqlite3.OperationalError:
            return False

    def _get_resource_conflicts(self, exclusive_resource: str) -> int:
        """Check if exclusive resource is already in use.

        Args:
            exclusive_resource: Resource name (e.g., "p40")

        Returns:
            Number of tasks currently using this resource
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get worker profile for the resource
            worker = self._worker_registry.get_worker_by_resource(exclusive_resource)
            if not worker:
                conn.close()
                return 0

            # Count running tasks for workers sharing this resource
            # (same exclusive_resource). Exclude retry_wait and other non-running states.
            cursor.execute("""
                SELECT COUNT(DISTINCT t.id) FROM tasks t
                INNER JOIN task_attempts a ON t.id = a.task_id
                INNER JOIN workers w ON a.worker_profile = w.profile
                WHERE t.state = 'running'
                AND w.exclusive_resource = ?
            """, (exclusive_resource,))

            count = cursor.fetchone()[0]
            conn.close()
            return count
        except sqlite3.OperationalError:
            return 0

    def _emit_event_in_transaction(self, cursor, task_id: str, event_type: str, from_state: Optional[str] = None,
                                   to_state: Optional[str] = None, details: Optional[str] = None):
        """Record event within the same transaction (avoids self-lock deadlock)."""
        try:
            cursor.execute("""
                INSERT INTO task_events (task_id, event_type, from_state, to_state, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                task_id, event_type, from_state, to_state, details,
                datetime.now(timezone.utc).isoformat(),
            ))
        except sqlite3.Error:
            pass  # Event logging failures are non-fatal

    def start_task(self, task_id: str, worker_profile: str,
                   model_profile: Optional[str] = None) -> bool:
        """Mark a task as running with atomic P40 resource locking.

        Uses BEGIN IMMEDIATE to acquire a reserved lock, then performs all
        operations (resource check, state transition, event logging) within
        the same transaction. This prevents the race condition where two
        different tasks could both see 0 conflicts and both start on the
        same P40 resource.

        Args:
            task_id: Task ID
            worker_profile: Worker profile that will process the task
            model_profile: Model profile to use

        Returns:
            True if successful
        """
        import time

        # Retry on lock contention (max 3 attempts with exponential backoff)
        for attempt in range(3):
            try:
                conn = self._get_connection()
                cursor = conn.cursor()

                # BEGIN IMMEDIATE acquires reserved lock, blocking other writers immediately
                cursor.execute("BEGIN IMMEDIATE")

                try:
                    # Check task is claimed (within transaction)
                    cursor.execute("SELECT state, kind FROM tasks WHERE id = ?", (task_id,))
                    row = cursor.fetchone()
                    if not row or row["state"] != "claimed":
                        conn.rollback()
                        conn.close()
                        return False

                    # Check worker capability if worker_profile provided
                    if worker_profile:
                        task_kind = row["kind"]
                        worker = self._worker_registry.get_worker(worker_profile)
                        if not worker:
                            conn.rollback()
                            conn.close()
                            return False

                        if worker.status != WorkerStatus.AVAILABLE:
                            self._emit_event_in_transaction(cursor, task_id, "worker_failed",
                                                           from_state=None, to_state=None,
                                                           details=f"worker={worker_profile} status={worker.status.value}")
                            conn.rollback()
                            conn.close()
                            return False

                        # ATOMIC RESOURCE CHECK: count running tasks with same exclusive_resource
                        # Using same cursor/connection as main transaction
                        if worker.exclusive_resource:
                            cursor.execute("""
                                SELECT COUNT(*) FROM tasks t
                                INNER JOIN task_attempts a ON t.id = a.task_id
                                INNER JOIN workers w ON a.worker_profile = w.profile
                                WHERE t.state = 'running'
                                AND w.exclusive_resource = ?
                            """, (worker.exclusive_resource,))
                            conflicts = cursor.fetchone()[0]
                            if conflicts > 0:
                                self._emit_event_in_transaction(cursor, task_id, "worker_failed",
                                                               from_state=None, to_state=None,
                                                               details=f"exclusive_resource={worker.exclusive_resource} already in_use")
                                conn.rollback()
                                conn.close()
                                return False

                        # Check capability matches
                        if worker.capability:
                            worker_caps = [cap.strip().lower() for cap in worker.capability.split(",")]
                            matched = any(
                                cap in task_kind.lower() or task_kind.lower() in cap
                                for cap in worker_caps
                            )
                            if not matched:
                                self._emit_event_in_transaction(cursor, task_id, "worker_failed",
                                                               from_state=None, to_state=None,
                                                               details=f"capability={worker.capability} doesn't match task_kind={task_kind}")
                                conn.rollback()
                                conn.close()
                                return False

                    # ATOMIC TRANSITION: close old attempt, update state, record new attempt
                    cursor.execute("""
                        UPDATE task_attempts SET end_at = ?
                        WHERE task_id = ? AND end_at IS NULL
                    """, (datetime.now(timezone.utc).isoformat(), task_id))

                    cursor.execute("""
                        UPDATE tasks SET state = 'running', updated_at = ?
                        WHERE id = ? AND state = 'claimed'
                    """, (datetime.now(timezone.utc).isoformat(), task_id))

                    if cursor.rowcount == 0:
                        conn.rollback()
                        conn.close()
                        return False

                    cursor.execute("""
                        INSERT INTO task_attempts (task_id, worker_profile, model_profile, start_at, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        task_id, worker_profile, model_profile,
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ))

                    # Emit event within same transaction (no self-lock!)
                    self._emit_event_in_transaction(cursor, task_id, "task_started",
                                                   from_state="claimed", to_state="running",
                                                   details=f"worker={worker_profile}, model={model_profile}")

                    conn.commit()
                    conn.close()
                    return True

                except sqlite3.Error:
                    conn.rollback()
                    conn.close()
                    return False

            except sqlite3.OperationalError as e:
                conn.close()
                # Lock contention - retry with exponential backoff
                if "database is locked" in str(e) and attempt < 2:
                    time.sleep(0.05 * (2 ** attempt))  # 50ms, 100ms, 200ms
                    continue
                return False

        return False

    def complete_task(self, task_id: str, result: Dict[str, Any] = None,
                      error: Optional[str] = None, failure_class: Optional[str] = None) -> bool:
        """Mark a task as completed, closing all open attempts.

        Args:
            task_id: Task ID
            result: Task result data
            error: Error message if failed
            failure_class: Classification of failure (if applicable)

        Returns:
            True if successful
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Check task is running
            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row or row["state"] != "running":
                conn.close()
                return False

            try:
                # Update task
                to_state = "succeeded" if error is None else "failed_terminal"
                cursor.execute("""
                UPDATE tasks SET state = ?, error = ?, updated_at = ?
                WHERE id = ?
                """, (to_state, error, datetime.now(timezone.utc).isoformat(), task_id))

                # Close ALL open attempts (fixes the multiple-attempt bug)
                cursor.execute("""
                UPDATE task_attempts SET end_at = ?, result = ?, failure_class = ?
                WHERE task_id = ? AND end_at IS NULL
                """, (datetime.now(timezone.utc).isoformat(),
                      self._json_dump(result) if result else None,
                      failure_class, task_id))

                self._emit_event(task_id, "task_completed", from_state="running", to_state=to_state,
                                details=f"result={'success' if error is None else 'failure'}")

                conn.commit()
                conn.close()
                return True

            except sqlite3.Error:
                conn.close()
                return False

        except sqlite3.OperationalError:
            return False

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task data or None
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                return None

            return self._row_to_dict(row)
        except sqlite3.OperationalError:
            return None

    def get_tasks_by_state(self, state: str, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        """Return durable tasks in one state for operator workflows.

        Args:
            state: Task state to filter by
            limit: Maximum number of tasks to return. Use limit=None for unlimited results.

        Returns:
            List of task dicts matching the state
        """
        if not state or (limit is not None and limit < 1):
            return []
        try:
            conn = self._get_connection()
            if limit is None:
                # No limit - fetch all tasks in state
                rows = conn.execute(
                    """SELECT * FROM tasks
                       WHERE state = ?
                       ORDER BY updated_at ASC, id ASC""",
                    (state,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM tasks
                       WHERE state = ?
                       ORDER BY updated_at ASC, id ASC
                       LIMIT ?""",
                    (state, limit),
                ).fetchall()
            conn.close()
            return [self._row_to_dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    def get_next_task(self) -> Optional[Dict[str, Any]]:
        """Get next task respecting policies and review priority.

        Uses get_pending_tasks() to respect policy schedule windows,
        then returns the highest priority task (review-tagged tasks first).

        Returns:
            Task dict with 'id', 'kind', 'payload', 'mode', 'priority' or None
        """
        # Get pending tasks respecting policy (up to limit=1)
        tasks = self.get_pending_tasks(limit=1)
        return tasks[0] if tasks else None

    def select_worker_for_task(self, task: Dict[str, Any]) -> Optional[str]:
        """Select an available worker without crossing task boundaries.

        Vision is deliberately an explicit P40 capability. Immediate coding
        also stays on the P40; non-urgent batch/nightly coding is assigned to
        the isolated CPU/RAM slow coder. Review tasks use the configured review
        worker and do not silently fall back to the P40 or GTX when that worker
        is unavailable.
        """
        if task.get("review_tag") or task.get("review_worker"):
            profile = task.get("review_worker") or "air-review"
        else:
            payload = task.get("payload") or {}
            requested_profile = task.get("worker_profile") or payload.get("worker_profile")
            if requested_profile:
                profile = requested_profile
            elif task.get("kind") == "vision":
                profile = "p40-vision"
            elif task.get("kind") == "coding":
                nightly = task.get("schedule_type") == "nightly"
                batch = task.get("mode") in {"batch", "maintenance"}
                profile = "slow-coder" if nightly or batch else "p40-coding"
            else:
                profile = None
            if profile is None:
                profile = "gtx-chat" if task.get("kind") in {
                    "query", "conversation", "general"
                } else None
        if profile is None:
            return None
        worker = self._worker_registry.get_worker(profile)
        if worker is None or worker.status != WorkerStatus.AVAILABLE:
            return None
        return worker.profile

    def get_retry_wait_task(self) -> Optional[Dict[str, Any]]:
        """Get next retry_wait task that has passed its retry delay.

        This is used by the daemon to find tasks ready for retry. The actual
        scheduling policy (e.g., vision IMAGE_WINDOW) is applied when the task
        is promoted to queued and selected via get_next_task().

        Returns:
            Task dict with 'id', 'kind', 'payload', 'mode', 'priority' or None
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get earliest retry_wait task that has passed its retry delay
            # Note: Use strftime to compare date/time only, ignoring microseconds
            cursor.execute("""
                SELECT id, kind, mode, priority FROM tasks
                WHERE state = 'retry_wait'
                AND (retry_at IS NULL OR strftime('%Y-%m-%d %H:%M:%S', retry_at) <= strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc'))
                ORDER BY priority DESC, id ASC
                LIMIT 1
            """)

            row = cursor.fetchone()
            if not row:
                conn.close()
                return None

            task_id = row["id"]
            task_kind = row["kind"]
            task_mode = row["mode"]
            task_priority = row["priority"]

            conn.close()
            return {
                "id": task_id,
                "kind": task_kind,
                "mode": task_mode,
                "priority": task_priority,
            }

        except sqlite3.Error:
            logger.exception("Error getting retry_wait task")
            return False

    def get_pending_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending (queued) tasks, respecting policy schedule.

        Policy rules:
        - Immediate tasks: Always allowed (outranks all scheduled work)
        - Vision tasks: IMAGE_WINDOW only (00:00-06:00)
        - Batch tasks: BATCH_WINDOW or IMAGE_WINDOW (after images empty)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get pending vision task count for policy decision
            # Count queued, claimed, and running vision tasks to determine if image queue is drained
            cursor.execute("""
                SELECT COUNT(*) FROM tasks
                WHERE state IN ('queued', 'claimed', 'running')
                  AND (mode = 'vision' OR kind = 'vision')
            """)
            pending_vision = cursor.fetchone()[0]

            # Get current schedule window
            current_window = self._policy.get_current_window(pending_vision=pending_vision)

            # Build query based on window
            # IMMEDIATE tasks always allowed
            # VISION only during IMAGE_WINDOW
            # BATCH during IMAGE_WINDOW (after images) or BATCH_WINDOW

            if current_window == ScheduleWindow.IMAGE_WINDOW:
                # Image window: vision + immediate
                cursor.execute("""
                SELECT * FROM tasks
                WHERE state = 'queued'
                  AND (mode IN ('vision', 'immediate')
                       OR schedule_type = 'immediate')
                ORDER BY
                    CASE WHEN mode = 'immediate' THEN 0 ELSE 1 END,
                    CASE WHEN kind IN ('coding', 'vision')
                              AND COALESCE(CAST(review_tag AS INTEGER), 0) = 0 THEN 0 ELSE 1 END,
                    priority DESC, created_at ASC
                LIMIT ?
                """, (limit,))
            elif current_window == ScheduleWindow.BATCH_WINDOW:
                # Batch window: batch + immediate
                cursor.execute("""
                SELECT * FROM tasks
                WHERE state = 'queued'
                  AND (mode IN ('batch', 'immediate')
                       OR schedule_type IN ('batch', 'immediate'))
                ORDER BY
                    CASE WHEN kind IN ('coding', 'vision')
                              AND COALESCE(CAST(review_tag AS INTEGER), 0) = 0 THEN 0 ELSE 1 END,
                    CASE WHEN mode = 'immediate' THEN 0 ELSE 1 END,
                    priority DESC, created_at ASC
                LIMIT ?
                """, (limit,))
            else:
                # RESTRICTED: only immediate tasks
                cursor.execute("""
                SELECT * FROM tasks
                WHERE state = 'queued'
                  AND (mode = 'immediate' OR schedule_type = 'immediate')
                ORDER BY
                    CASE WHEN kind IN ('coding', 'vision')
                              AND COALESCE(CAST(review_tag AS INTEGER), 0) = 0 THEN 0 ELSE 1 END,
                    priority DESC, created_at ASC
                LIMIT ?
                """, (limit,))

            rows = cursor.fetchall()
            conn.close()

            return [self._row_to_dict(row) for row in rows]
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            return []

    def get_task_events(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get task event history.

        Args:
            task_id: Task ID
            limit: Maximum events to return

        Returns:
            List of event data
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """, (task_id, limit))

            rows = cursor.fetchall()
            conn.close()

            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    def get_queue_depths(self) -> Dict[str, int]:
        """Get queue depths by state.

        Returns:
            Dictionary of state counts
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
            SELECT state, COUNT(*) as count
            FROM tasks
            GROUP BY state
            """)

            rows = cursor.fetchall()
            conn.close()

            return {row["state"]: row["count"] for row in rows}
        except sqlite3.OperationalError:
            return {}

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert database row to dictionary."""
        result = dict(row)
        result["payload"] = self._json_load(result.get("payload"))
        if "review_tag" in result:
            result["review_tag"] = str(result["review_tag"]).lower() in {
                "1", "true", "yes"
            }
        return result

    def _json_dump(self, data: Any) -> str:
        """Serialize data to JSON string."""
        import json
        return json.dumps(data)

    def _json_load(self, json_str: str) -> Any:
        """Deserialize JSON string to data."""
        import json
        if not json_str:
            return None
        return json.loads(json_str)

    def retry_task(self, task_id: str, delay_seconds: int = 60) -> bool:
        """Schedule task for retry with proper state enforcement and delay.

        Args:
            task_id: Task ID to retry
            delay_seconds: Seconds to wait before retry

        Returns:
            True if retry scheduled
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Verify task is in running state (only legal source per STATE_TRANSITIONS)
            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            from_state = row["state"]

            # Only running can transition to retry_wait per STATE_TRANSITIONS
            if from_state != "running":
                conn.close()
                return False

            # Validate transition is legal
            if not self._validate_transition(task_id, from_state, "retry_wait"):
                conn.close()
                return False

            # Close the current open attempt before retry
            self._close_current_attempt(task_id)

            # Calculate retry time
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

            to_state = "retry_wait"
            cursor.execute("""
                UPDATE tasks SET state = ?, retry_at = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, retry_at.isoformat(), datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                self._emit_event(task_id, "retry_scheduled",
                               from_state=from_state, to_state=to_state,
                               details=f"delay={delay_seconds}s, retry_at={retry_at.isoformat()}")
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task, enforcing STATE_TRANSITIONS table.

        Args:
            task_id: Task ID to cancel

        Returns:
            True if cancelled
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Lock the state read and transition together.  In particular, this
            # prevents cancellation from observing ``claimed`` and then
            # overwriting a concurrent ``claimed -> running`` transition.
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False

            from_state = row["state"]
            to_state = "cancelled"
            if not self._validate_transition(task_id, from_state, to_state):
                conn.rollback()
                return False

            cursor.execute("""
                UPDATE tasks SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(),
                  task_id, from_state))
            if cursor.rowcount != 1:
                conn.rollback()
                return False

            self._emit_event_in_transaction(
                cursor, task_id, "task_cancelled",
                from_state=from_state, to_state=to_state,
            )
            conn.commit()
            return True

        except sqlite3.Error:
            if conn is not None:
                conn.rollback()
            return False
        finally:
            if conn is not None:
                conn.close()

    def requeue_claimed_to_retry_wait(self, task_id: str) -> bool:
        """Transition claimed → retry_wait (for when task cannot start).

        Args:
            task_id: Task ID

        Returns:
            True if requeued
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "claimed":
                conn.close()
                return False

            to_state = "retry_wait"
            cursor.execute("""
                UPDATE tasks SET state = ?, retry_at = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(),
                  datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                self._emit_event(task_id, "task_retry_wait",
                               from_state="claimed", to_state=to_state)
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def requeue_retry_wait(self, task_id: str) -> bool:
        """Transition retry_wait → queued with delay enforcement.

        Args:
            task_id: Task ID to requeue

        Returns:
            True if requeued
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state, retry_at FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "retry_wait":
                conn.close()
                return False

            # Check if retry delay has elapsed
            if row["retry_at"]:
                retry_at = datetime.fromisoformat(row["retry_at"])
                if datetime.now(timezone.utc) < retry_at:
                    conn.close()
                    return False  # Still in delay period

            to_state = "queued"
            cursor.execute("""
                UPDATE tasks SET state = ?, retry_at = NULL, updated_at = ?
                WHERE id = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                self._emit_event(task_id, "task_requeued",
                               from_state="retry_wait", to_state=to_state)
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def requeue_awaiting_review(self, task_id: str) -> bool:
        """Transition awaiting_review → queued.

        Args:
            task_id: Task ID to requeue

        Returns:
            True if requeued
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "awaiting_review":
                conn.close()
                return False

            to_state = "queued"
            cursor.execute("""
                UPDATE tasks SET state = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                self._emit_event(task_id, "task_requeued",
                               from_state="awaiting_review", to_state=to_state)
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def approve_awaiting_review(self, task_id: str) -> bool:
        """Transition an approved review directly to succeeded."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row or row["state"] != "awaiting_review":
                conn.close()
                return False
            cursor.execute(
                """UPDATE tasks SET state = 'succeeded', updated_at = ?
                   WHERE id = ? AND state = 'awaiting_review'""",
                (datetime.now(timezone.utc).isoformat(), task_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                conn.close()
                return False
            self._emit_event(
                task_id,
                "task_completed",
                from_state="awaiting_review",
                to_state="succeeded",
                details="result=review_approved",
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.OperationalError:
            return False

    def transition_awaiting_review_to_failed(self, task_id: str, error: Optional[str] = None) -> bool:
        """Transition awaiting_review → failed_terminal.

        Args:
            task_id: Task ID
            error: Optional error message

        Returns:
            True if transitioned
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "awaiting_review":
                conn.close()
                return False

            to_state = "failed_terminal"
            cursor.execute("""
                UPDATE tasks SET state = ?, error = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, error, datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                self._emit_event(task_id, "task_completed",
                               from_state="awaiting_review", to_state=to_state,
                               details="result=failure")
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def transition_running_to_retry_wait(self, task_id: str) -> bool:
        """Transition running → retry_wait, closing current attempt.

        Args:
            task_id: Task ID

        Returns:
            True if transitioned
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "running":
                conn.close()
                return False

            # Close current attempt before transitioning
            self._close_current_attempt(task_id)

            to_state = "retry_wait"
            cursor.execute("""
                UPDATE tasks SET state = ?, retry_at = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(),
                  datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                # Emit retry_wait event
                self._emit_event(task_id, "task_retry_wait",
                               from_state="running", to_state=to_state)
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False

    def transition_running_to_awaiting_review(self, task_id: str) -> bool:
        """Transition running → awaiting_review, closing current attempt.

        Args:
            task_id: Task ID

        Returns:
            True if transitioned
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False

            if row["state"] != "running":
                conn.close()
                return False

            # Close current attempt before transitioning
            self._close_current_attempt(task_id)

            to_state = "awaiting_review"
            cursor.execute("""
                UPDATE tasks SET state = ?, updated_at = ?
                WHERE id = ?
            """, (to_state, datetime.now(timezone.utc).isoformat(), task_id))

            if cursor.rowcount > 0:
                # Emit awaiting_review event (not task_started which is misleading)
                self._emit_event(task_id, "task_awaiting_review",
                               from_state="running", to_state=to_state)
                conn.commit()

            conn.close()
            return True

        except sqlite3.OperationalError:
            return False
