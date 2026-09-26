"""Core scheduler implementation."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, List
import json
import os

@dataclass
class SchedulerConfig:
    """Scheduler configuration."""
    queue_file: str = "/mnt/scratch/scheduler/queue.json"
    state_file: str = "/mnt/scratch/scheduler/state.json"
    max_concurrent: int = 1
    poll_interval: float = 5.0
    
class Scheduler:
    """Task scheduler with queue management."""
    
    def __init__(self, config: Optional[SchedulerConfig] = None):
        self.config = config or SchedulerConfig()
        self._queue: List[dict] = []
        self._state: dict = {}
        self._load_state()
        
    def _load_state(self):
        """Load scheduler state from disk."""
        if os.path.exists(self.config.state_file):
            with open(self.config.state_file, 'r') as f:
                self._state = json.load(f)
            self._queue = self._state.get('queue', [])
    
    def _save_state(self):
        """Save scheduler state to disk."""
        self._state['queue'] = self._queue
        self._state['last_updated'] = datetime.utcnow().isoformat()
        os.makedirs(os.path.dirname(self.config.state_file), exist_ok=True)
        with open(self.config.state_file, 'w') as f:
            json.dump(self._state, f, indent=2)
    
    def add_task(self, task_id: str, task_type: str, payload: dict, 
                 priority: int = 5, scheduled_at: Optional[datetime] = None):
        """Add a task to the queue."""
        task = {
            'task_id': task_id,
            'task_type': task_type,
            'payload': payload,
            'priority': priority,
            'status': 'pending',
            'scheduled_at': scheduled_at.isoformat() if scheduled_at else None,
            'created_at': datetime.utcnow().isoformat(),
            'started_at': None,
            'completed_at': None,
            'result': None,
            'error': None,
        }
        self._queue.append(task)
        self._save_state()
        return task_id
    
    def get_queue(self) -> List[dict]:
        """Get all tasks in the queue."""
        return sorted(self._queue, key=lambda x: (x['priority'], x['created_at']))
    
    def get_pending(self) -> List[dict]:
        """Get pending tasks."""
        return [t for t in self._queue if t['status'] == 'pending']
    
    def get_running(self) -> List[dict]:
        """Get running tasks."""
        return [t for t in self._queue if t['status'] == 'running']
    
    def get_completed(self) -> List[dict]:
        """Get completed tasks."""
        return [t for t in self._queue if t['status'] == 'completed']
    
    def start_task(self, task_id: str) -> bool:
        """Start a task."""
        for task in self._queue:
            if task['task_id'] == task_id and task['status'] == 'pending':
                task['status'] = 'running'
                task['started_at'] = datetime.utcnow().isoformat()
                self._save_state()
                return True
        return False
    
    def complete_task(self, task_id: str, result: dict = None, error: str = None):
        """Mark a task as completed."""
        for task in self._queue:
            if task['task_id'] == task_id and task['status'] == 'running':
                task['status'] = 'completed'
                task['completed_at'] = datetime.utcnow().isoformat()
                task['result'] = result
                task['error'] = error
                self._save_state()
                return True
        return False
    
    def process_next(self, handler: Callable) -> bool:
        """Process the next pending task using the provided handler."""
        pending = self.get_pending()
        if not pending:
            return False
        
        # Get highest priority task
        task = min(pending, key=lambda x: (x['priority'], x['created_at']))
        
        if self.start_task(task['task_id']):
            try:
                result = handler(task)
                self.complete_task(task['task_id'], result=result)
                return True
            except Exception as e:
                self.complete_task(task['task_id'], error=str(e))
                return False
        return False
