"""Queue management utilities."""
from typing import List, Dict, Any
import json
import os

class QueueManager:
    """Manages task queues with persistence."""
    
    def __init__(self, queue_file: str = "/mnt/scratch/scheduler/queue.json"):
        self.queue_file = queue_file
        self._queues: Dict[str, List[Dict[str, Any]]] = {}
        self._load_queues()
    
    def _load_queues(self):
        """Load queues from disk."""
        if os.path.exists(self.queue_file):
            with open(self.queue_file, 'r') as f:
                self._queues = json.load(f)
    
    def _save_queues(self):
        """Save queues to disk."""
        os.makedirs(os.path.dirname(self.queue_file), exist_ok=True)
        with open(self.queue_file, 'w') as f:
            json.dump(self._queues, f, indent=2)
    
    def add_to_queue(self, queue_name: str, item: Dict[str, Any]):
        """Add an item to a queue."""
        if queue_name not in self._queues:
            self._queues[queue_name] = []
        self._queues[queue_name].append({
            'item': item,
            'added_at': __import__('datetime').datetime.utcnow().isoformat(),
        })
        self._save_queues()
    
    def get_queue(self, queue_name: str) -> List[Dict[str, Any]]:
        """Get all items in a queue."""
        return self._queues.get(queue_name, [])
    
    def pop_from_queue(self, queue_name: str) -> Dict[str, Any]:
        """Remove and return the first item from a queue."""
        items = self._queues.get(queue_name, [])
        if items:
            item = items.pop(0)
            self._queues[queue_name] = items
            self._save_queues()
            return item['item']
        return None
    
    def clear_queue(self, queue_name: str):
        """Clear a queue."""
        if queue_name in self._queues:
            self._queues[queue_name] = []
            self._save_queues()
    
    def queue_size(self, queue_name: str) -> int:
        """Get the size of a queue."""
        return len(self._queues.get(queue_name, []))
