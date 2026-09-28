"""Queue status and cancellation API for GTX broker."""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs
import sqlite3

from gtx_broker.scheduler import Scheduler, SchedulerConfig


class StatusAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for queue status and cancellation API."""
    
    def log_message(self, format, *args):
        """Suppress default logging."""
        pass
    
    def do_GET(self):
        """Handle GET requests for task status."""
        parsed = urlparse(self.path)
        
        if parsed.path == "/status":
            # List all tasks (admin view)
            self._list_tasks()
        elif parsed.path.startswith("/status/"):
            # Get specific task status
            task_id = parsed.path[len("/status/"):]
            self._get_task_status(task_id)
        elif parsed.path == "/queue":
            # Get queue statistics
            self._get_queue_status()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
    
    def do_POST(self):
        """Handle POST requests for task cancellation."""
        parsed = urlparse(self.path)
        
        if parsed.path == "/cancel":
            # Cancel a task
            self._cancel_task()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
    
    def _get_scheduler(self) -> Scheduler:
        """Get scheduler instance."""
        db_path = "/mnt/scratch/gtx-images/metadata/tasks.db"
        config = SchedulerConfig(db_path=db_path)
        return Scheduler(config)
    
    def _get_task_status(self, task_id: str):
        """Get status of a specific task."""
        scheduler = self._get_scheduler()
        task = scheduler.get_task(task_id)
        
        if not task:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Task not found",
                "task_id": task_id
            }).encode())
            return
        
        # Get recent events
        events = self._get_task_events(task_id, limit=5)
        
        response = {
            "task_id": task_id,
            "state": task.get("state"),
            "kind": task.get("kind"),
            "priority": task.get("priority"),
            "mode": task.get("mode"),
            "schedule_type": task.get("schedule_type"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "events": events,
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode())
    
    def _get_task_events(self, task_id: str, limit: int = 5) -> list:
        """Get recent events for a task."""
        try:
            scheduler = self._get_scheduler()
            conn = scheduler._get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT event_type, from_state, to_state, details, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (task_id, limit))
            
            events = []
            for row in cursor.fetchall():
                events.append({
                    "event_type": row[0],
                    "from_state": row[1],
                    "to_state": row[2],
                    "details": row[3],
                    "created_at": row[4],
                })
            
            conn.close()
            return events
        except Exception as e:
            return [{"error": str(e)}]
    
    def _list_tasks(self):
        """List all tasks (admin view)."""
        scheduler = self._get_scheduler()
        
        # Get all tasks
        conn = scheduler._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, kind, state, priority, mode, schedule_type, 
                   created_at, updated_at
            FROM tasks
            ORDER BY created_at DESC
            LIMIT 50
        """)
        
        tasks = []
        for row in cursor.fetchall():
            tasks.append({
                "task_id": row[0],
                "kind": row[1],
                "state": row[2],
                "priority": row[3],
                "mode": row[4],
                "schedule_type": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            })
        
        conn.close()
        
        response = {
            "count": len(tasks),
            "tasks": tasks,
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode())
    
    def _get_queue_status(self):
        """Get queue statistics."""
        parsed = urlparse(self.path)
        scheduler = self._get_scheduler()
        
        conn = scheduler._get_connection()
        cursor = conn.cursor()
        
        # Get counts by state
        cursor.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state")
        state_counts = dict(cursor.fetchall())
        
        # Get queue position for a specific task if provided
        params = parse_qs(parsed.query)
        task_id_param = params.get("task_id", [None])[0] if "task_id" in params else None
        
        if task_id_param:
            # Calculate queue position
            cursor.execute("""
                SELECT COUNT(*) FROM tasks
                WHERE state = 'queued'
                AND (priority > (SELECT priority FROM tasks WHERE id = ?)
                     OR (priority = (SELECT priority FROM tasks WHERE id = ?)
                         AND created_at < (SELECT created_at FROM tasks WHERE id = ?)))
            """, (task_id_param, task_id_param, task_id_param))
            queue_position = cursor.fetchone()[0] + 1
        else:
            queue_position = None
        
        conn.close()
        
        response = {
            "queue_stats": state_counts,
            "total_queued": state_counts.get("queued", 0),
            "total_claimed": state_counts.get("claimed", 0),
            "total_running": state_counts.get("running", 0),
            "total_completed": state_counts.get("succeeded", 0),
            "total_failed": state_counts.get("failed_terminal", 0) + state_counts.get("cancelled", 0),
            "queue_position": queue_position,
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode())
    
    def _cancel_task(self):
        """Cancel a task."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            
            task_id = data.get("task_id")
            if not task_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "task_id is required",
                    "usage": "POST /cancel with {\"task_id\": \"<task_id>\"}"
                }).encode())
                return
            
            scheduler = self._get_scheduler()
            
            # Try to cancel
            success = scheduler.cancel_task(task_id)
            
            if success:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": f"Task {task_id} cancelled",
                    "task_id": task_id,
                }).encode())
            else:
                task = scheduler.get_task(task_id)
                if not task:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": "Task not found",
                        "task_id": task_id,
                    }).encode())
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": f"Cannot cancel task in state: {task.get('state')}",
                        "allowed_states": ["queued", "claimed"],
                        "current_state": task.get("state"),
                        "task_id": task_id,
                    }).encode())
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": f"Invalid request body: {e}"
            }).encode())


def start_status_api(host: str = "127.0.0.1", port: int = 11439) -> None:
    """Start the status API server in a background thread."""
    server = HTTPServer((host, port), StatusAPIHandler)
    thread = __import__('threading').Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Status API listening on {host}:{port}")


if __name__ == "__main__":
    import sys
    print("GTX Broker Status API")
    print("Endpoints:")
    print("  GET  /status             - List recent tasks")
    print("  GET  /status/<task_id>   - Get task status")
    print("  GET  /queue              - Queue statistics")
    print("  GET  /queue?task_id=xxx  - Get queue position for task")
    print("  POST /cancel             - Cancel a task")
    print("\nStarting server...")
    start_status_api()
