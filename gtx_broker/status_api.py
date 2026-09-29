"""HTTP API for scheduler status, queue info, and task cancellation.

This module provides a single HTTP server instance that shares the scheduler
and database connection across all requests, avoiding per-request initialization.
"""

import json
import logging
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, Optional

from gtx_broker.scheduler import Scheduler

logger = logging.getLogger(__name__)


class StatusAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for scheduler status and queue management."""

    scheduler: Scheduler  # Set by server on each request
    server_instance: "StatusAPI" = None  # Reference to server for shutdown

    def log_message(self, format, *args):
        """Suppress default HTTP logging for cleaner output."""
        pass

    def _send_json_response(self, data: Dict[str, Any], status: int = 200):
        """Send JSON response with proper headers."""
        response_body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def _send_error_response(self, message: str, status: int = 400):
        """Send error response."""
        self._send_json_response({"error": message}, status)

    def do_GET(self):
        """Handle GET requests."""
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path.startswith("/status/"):
            # Get single task status
            task_id = urllib.parse.unquote(parsed.path[len("/status/"):])
            self._handle_task_status(task_id)
        elif parsed.path == "/queue":
            # Get queue statistics or position
            if "task_id" in query:
                task_id = query["task_id"][0]
                self._handle_queue_position(task_id)
            else:
                self._handle_queue_stats()
        else:
            self._send_error_response("Not found", 404)

    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/cancel":
            self._handle_cancel()
        else:
            self._send_error_response("Not found", 404)

    def _handle_task_status(self, task_id: str):
        """Get status for a specific task."""
        task = self.scheduler.get_task(task_id)
        if not task:
            self._send_error_response("Task not found", 404)
            return

        # Get recent events
        events = self.scheduler.get_task_events(task_id, limit=5)

        response = {
            "task_id": task_id,
            "state": task.get("state"),
            "kind": task.get("kind"),
            "priority": task.get("priority"),
            "mode": task.get("mode"),
            "schedule_type": task.get("schedule_type"),
            "worker_profile": task.get("worker_profile"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "events": events,
        }
        self._send_json_response(response)

    def _handle_queue_stats(self):
        """Get queue statistics."""
        depths = self.scheduler.get_queue_depths()

        response = {
            "queued": depths.get("queued", 0),
            "claimed": depths.get("claimed", 0),
            "running": depths.get("running", 0),
            "succeeded": depths.get("succeeded", 0),
            "failed": depths.get("failed_terminal", 0),
            "total": sum(depths.values()),
        }
        self._send_json_response(response)

    def _handle_queue_position(self, task_id: str):
        """Get queue position for a specific task.

        Only returns position for tasks in 'queued' state.
        Returns None for tasks not in queue.
        """
        task = self.scheduler.get_task(task_id)
        if not task:
            self._send_error_response("Task not found", 404)
            return

        state = task.get("state")
        if state != "queued":
            self._send_json_response({
                "task_id": task_id,
                "state": state,
                "queue_position": None,
                "note": f"Not in queue (state: {state})",
            })
            return

        # Count tasks with higher priority that were created earlier
        # This matches the scheduler's priority-based ordering
        # Use limit=None to avoid the 100-task cap bug
        all_queued = self.scheduler.get_tasks_by_state("queued", limit=None)
        my_priority = task.get("priority", 0)
        my_created = task.get("created_at", "")

        position = 1
        for other in all_queued:
            if other["id"] == task_id:
                continue
            other_priority = other.get("priority", 0)
            other_created = other.get("created_at", "")

            # Higher priority first, then earlier creation time
            if other_priority > my_priority:
                position += 1
            elif other_priority == my_priority and other_created < my_created:
                position += 1

        self._send_json_response({
            "task_id": task_id,
            "state": state,
            "queue_position": position,
            "total_queued": len(all_queued),
        })

    def _handle_cancel(self):
        """Cancel a task."""
        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_error_response("Request body required", 400)
            return

        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_error_response("Invalid JSON", 400)
            return

        task_id = data.get("task_id")
        if not task_id:
            self._send_error_response("task_id required", 400)
            return

        # Check if task exists
        task = self.scheduler.get_task(task_id)
        if not task:
            self._send_error_response("Task not found", 404)
            return

        # Check if can be cancelled - only queued or claimed
        state = task.get("state")
        if state not in ("queued", "claimed"):
            self._send_error_response(
                f"Cannot cancel task in state '{state}'", 400
            )
            return

        # Attempt cancellation
        if self.scheduler.cancel_task(task_id):
            self._send_json_response({
                "task_id": task_id,
                "success": True,
                "message": f"Task cancelled (was in state: {state})",
            })
        else:
            self._send_error_response("Cancellation failed", 500)


class StatusAPI:
    """HTTP API server wrapper with proper lifecycle management."""

    def __init__(self, scheduler: Scheduler, port: int = 11439):
        """Initialize API server.

        Args:
            scheduler: Shared scheduler instance
            port: Port to listen on
        """
        self.scheduler = scheduler
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> bool:
        """Start the HTTP server.

        Returns:
            True if server started successfully, False otherwise
        """
        with self._lock:
            if self._started:
                return True

            # Check if port is available
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", self.port))
                sock.close()
            except OSError as e:
                logger.error(f"Port {self.port} already in use: {e}")
                return False

            class ThreadedServer(ThreadingMixIn, HTTPServer):
                daemon_threads = True
                allow_reuse_address = True

            try:
                self.server = ThreadedServer(("127.0.0.1", self.port), StatusAPIHandler)
                StatusAPIHandler.scheduler = self.scheduler
                StatusAPIHandler.server_instance = self
                self._started = True
                logger.info("Status API server started on port %d", self.port)
                return True
            except Exception as e:
                logger.error("Failed to start status API server: %s", e)
                return False

    def run_forever(self):
        """Run server loop in current thread."""
        if self.server:
            self.server.serve_forever()
        else:
            logger.error("Server not started - cannot run")

    def shutdown(self):
        """Gracefully shut down the server."""
        with self._lock:
            server = self.server
            if server:
                server.shutdown()
                server.server_close()
                self.server = None
                self._started = False
                logger.info("Status API server stopped")
