"""Worker profile registry and dispatch boundary.

Defines worker profiles from the scheduler architecture:
- p40-coding: endpoint 11436, text/code tasks
- p40-vision: endpoint 11436, vision tasks (requires projector)
- gtx-chat: endpoint 11438, conversation/brokering only
- external-provider: only when explicitly requested
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from enum import Enum
import sqlite3
from pathlib import Path


class WorkerStatus(Enum):
    """Worker availability status."""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    MAINTENANCE = "maintenance"


@dataclass
class WorkerProfile:
    """Worker profile definition."""
    profile: str
    endpoint: str
    capability: str
    status: WorkerStatus = WorkerStatus.AVAILABLE
    exclusive_resource: Optional[str] = None
    max_concurrent: int = 1
    context_limit: Optional[int] = None
    model_profile: Optional[str] = None  # P40 model profile for switching
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "profile": self.profile,
            "endpoint": self.endpoint,
            "capability": self.capability,
            "status": self.status.value,
            "exclusive_resource": self.exclusive_resource,
            "max_concurrent": self.max_concurrent,
            "context_limit": self.context_limit,
            "model_profile": self.model_profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerProfile":
        """Create from dictionary."""
        return cls(
            profile=data["profile"],
            endpoint=data["endpoint"],
            capability=data["capability"],
            status=WorkerStatus(data.get("status", "available")),
            exclusive_resource=data.get("exclusive_resource"),
            max_concurrent=data.get("max_concurrent", 1),
            context_limit=data.get("context_limit"),
            model_profile=data.get("model_profile"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class WorkerRegistry:
    """Registry of worker profiles with SQLite persistence.

    Manages worker definitions, capabilities, and availability.
    """

    def __init__(self, db_path: Path):
        """Initialize worker registry.

        Args:
            db_path: Path to SQLite database (e.g., /mnt/scratch/gtx-images/metadata/tasks.db)
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize workers table in existing database."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            profile TEXT PRIMARY KEY,
            endpoint TEXT,
            capability TEXT,
            availability TEXT NOT NULL DEFAULT 'available',
            exclusive_resource TEXT,
            max_concurrent INTEGER DEFAULT 1,
            context_limit INTEGER,
            model_profile TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()
        conn.close()

    def register_worker(self, worker: WorkerProfile) -> bool:
        """Register or update a worker profile.

        Args:
            worker: Worker profile to register

        Returns:
            True if successful
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            cursor.execute("""
            INSERT OR REPLACE INTO workers (
                profile, endpoint, capability, availability,
                exclusive_resource, max_concurrent, context_limit,
                model_profile, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                worker.profile,
                worker.endpoint,
                worker.capability,
                worker.status.value,
                worker.exclusive_resource,
                worker.max_concurrent,
                worker.context_limit,
                worker.model_profile,
                worker.updated_at,
            ))

            conn.commit()
            return True

        finally:
            conn.close()

    def _insert_worker(self, worker: WorkerProfile) -> bool:
        """Insert worker profile if not exists (preserves existing status).

        This is used during initialization to avoid overwriting persisted status.

        Args:
            worker: Worker profile to insert

        Returns:
            True if inserted, False if already exists
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            cursor.execute("""
            INSERT INTO workers (
                profile, endpoint, capability, availability,
                exclusive_resource, max_concurrent, context_limit,
                model_profile, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile) DO NOTHING
            """, (
                worker.profile,
                worker.endpoint,
                worker.capability,
                worker.status.value,
                worker.exclusive_resource,
                worker.max_concurrent,
                worker.context_limit,
                worker.model_profile,
                worker.created_at,
                worker.updated_at,
            ))

            inserted = cursor.rowcount > 0
            conn.commit()
            return inserted

        finally:
            conn.close()

    def get_worker(self, profile: str) -> Optional[WorkerProfile]:
        """Get worker profile by name.

        Args:
            profile: Worker profile name

        Returns:
            WorkerProfile if found, None otherwise
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM workers WHERE profile = ?", (profile,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        columns = ["profile", "endpoint", "capability", "availability",
                   "exclusive_resource", "max_concurrent", "context_limit",
                   "model_profile", "created_at", "updated_at"]
        data = dict(zip(columns, row))
        # Map 'availability' column to 'status' key for WorkerProfile.from_dict
        data["status"] = data["availability"]
        del data["availability"]
        return WorkerProfile.from_dict(data)

    def get_all_workers(self) -> List[WorkerProfile]:
        """Get all registered workers.

        Returns:
            List of WorkerProfile
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM workers")
        rows = cursor.fetchall()
        conn.close()

        columns = ["profile", "endpoint", "capability", "availability",
                   "exclusive_resource", "max_concurrent", "context_limit",
                   "model_profile", "created_at", "updated_at"]
        
        # Map 'availability' column to 'status' key for all workers
        result = []
        for row in rows:
            data = dict(zip(columns, row))
            # Map 'availability' to 'status' for WorkerProfile.from_dict
            data["status"] = data["availability"]
            del data["availability"]
            result.append(WorkerProfile.from_dict(data))
        return result

    def update_status(self, profile: str, status: WorkerStatus) -> bool:
        """Update worker status.

        Args:
            profile: Worker profile name
            status: New status

        Returns:
            True if updated
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("""
        UPDATE workers SET availability = ?, updated_at = ?
        WHERE profile = ?
        """, (status.value, datetime.now(timezone.utc).isoformat(), profile))

        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()

        return updated

    def select_worker(self, capability: str, context_size: int = 65536) -> Optional[WorkerProfile]:
        """Select best available worker for a capability.

        Args:
            capability: Required capability (e.g., "text", "vision")
            context_size: Required context size in tokens

        Returns:
            Best matching WorkerProfile or None
        """
        workers = self.get_all_workers()

        # Filter by capability
        capable = [w for w in workers if capability.lower() in w.capability.lower()]

        # Filter by context limit
        if context_size:
            capable = [w for w in capable if w.context_limit is None or w.context_limit >= context_size]

        # Filter by availability
        available = [w for w in capable if w.status == WorkerStatus.AVAILABLE]

        if not available:
            return None

        # Return first available (could add more sophisticated selection)
        return available[0]


# Default workers from architecture
DEFAULT_WORKERS = [
    WorkerProfile(
        profile="p40-coding",
        endpoint="127.0.0.1:11436/v1",
        capability="text,code",
        status=WorkerStatus.AVAILABLE,
        context_limit=262144,
        model_profile="p40-coding",
    ),
    WorkerProfile(
        profile="p40-vision",
        endpoint="127.0.0.1:11436/v1",
        capability="vision",
        status=WorkerStatus.AVAILABLE,
        context_limit=65536,
        model_profile="p40-vision-qwen35",
    ),
    WorkerProfile(
        profile="gtx-chat",
        endpoint="127.0.0.1:11438/v1",
        capability="conversation,brokering",
        status=WorkerStatus.AVAILABLE,
        context_limit=65536,
    ),
    WorkerProfile(
        profile="external-provider",
        endpoint="",
        capability="explicit-only",
        status=WorkerStatus.UNAVAILABLE,
    ),
]


def initialize_workers(db_path: Path):
    """Initialize default worker profiles.

    Args:
        db_path: Path to SQLite database
    """
    registry = WorkerRegistry(db_path)

    for worker in DEFAULT_WORKERS:
        # Use INSERT only, not REPLACE, to preserve existing status
        registry._insert_worker(worker)
