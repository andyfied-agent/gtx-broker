"""Data models for the scheduler."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class TaskState:
    """Represents the state of a single task."""
    task_id: str
    task_type: str
    payload: Dict[str, Any]
    priority: int = 5
    status: TaskStatus = TaskStatus.PENDING
    scheduled_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'payload': self.payload,
            'priority': self.priority,
            'status': self.status.value,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'created_at': self.created_at.isoformat(),
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'result': self.result,
            'error': self.error,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskState':
        """Create from dictionary."""
        return cls(
            task_id=data['task_id'],
            task_type=data['task_type'],
            payload=data['payload'],
            priority=data.get('priority', 5),
            status=TaskStatus(data.get('status', 'pending')),
            scheduled_at=datetime.fromisoformat(data['scheduled_at']) if data.get('scheduled_at') else None,
            created_at=datetime.fromisoformat(data['created_at']),
            started_at=datetime.fromisoformat(data['started_at']) if data.get('started_at') else None,
            completed_at=datetime.fromisoformat(data['completed_at']) if data.get('completed_at') else None,
            result=data.get('result'),
            error=data.get('error'),
        )

@dataclass
class TaskQueue:
    """Represents a collection of tasks."""
    name: str
    tasks: list[TaskState] = field(default_factory=list)
    
    def add_task(self, task: TaskState):
        """Add a task to the queue."""
        self.tasks.append(task)
    
    def get_pending(self) -> list[TaskState]:
        """Get all pending tasks."""
        return [t for t in self.tasks if t.status == TaskStatus.PENDING]
    
    def get_running(self) -> list[TaskState]:
        """Get all running tasks."""
        return [t for t in self.tasks if t.status == TaskStatus.RUNNING]
    
    def get_completed(self) -> list[TaskState]:
        """Get all completed tasks."""
        return [t for t in self.tasks if t.status == TaskStatus.COMPLETED]
    
    def sort_by_priority(self):
        """Sort tasks by priority."""
        self.tasks.sort(key=lambda x: (x.priority, x.created_at))
