"""Integration tests for status API."""

import json
import shutil
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from threading import Thread

import uuid

from gtx_broker.scheduler import Scheduler, SchedulerConfig
from gtx_broker.daemon import SchedulerDaemon
from gtx_broker.status_api import StatusAPI


class TestStatusAPIIntegration(unittest.TestCase):
    """Integration tests for status API."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp(prefix="gtx-test-"))
        self.temp_incoming = self.temp_dir / "incoming"
        self.temp_incoming.mkdir()

        self.temp_db = self.temp_dir / f"test_{time.time()}.db"
        self.config = SchedulerConfig(db_path=str(self.temp_db))
        self.scheduler = Scheduler(self.config)

        # Create test tasks
        self.task_id = str(uuid.uuid4())
        self.scheduler.add_task(
            task_id=self.task_id,
            kind="vision",
            payload={"source": "test"},
            priority=10,
        )

        # Start API server
        self.api = StatusAPI(self.scheduler, port=0)
        self.assertTrue(self.api.start())

        # Get the actual port and start thread
        self.port = self.api.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.api_thread = Thread(target=self.api.run_forever, daemon=True)
        self.api_thread.start()

        # Give server time to start
        time.sleep(0.3)

    def tearDown(self):
        """Clean up test fixtures."""
        if self.api:
            self.api.shutdown()
        time.sleep(0.1)

    def test_status_endpoint(self):
        """Test GET /status/{task_id} works."""
        req = urllib.request.Request(f"{self.base_url}/status/{self.task_id}")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task_id"], self.task_id)
            self.assertEqual(data["state"], "queued")

    def test_status_endpoint_ignores_query_string(self):
        """Task IDs must come from the path, not from the complete request target."""
        req = urllib.request.Request(
            f"{self.base_url}/status/{self.task_id}?details=1"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task_id"], self.task_id)

    def test_cancel_queued_task(self):
        """Test POST /cancel works on queued tasks."""
        payload = json.dumps({"task_id": self.task_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/cancel",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertTrue(data["success"])

    def test_uses_scheduler_database(self):
        """Verify API uses scheduler's database path."""
        self.assertEqual(self.scheduler.config.db_path, str(self.temp_db))

    def test_queue_endpoint_with_task_id(self):
        """Test GET /queue?task_id=... returns position for queued task."""
        req = urllib.request.Request(f"{self.base_url}/queue?task_id={self.task_id}")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task_id"], self.task_id)
            self.assertEqual(data["state"], "queued")
            self.assertEqual(data["queue_position"], 1)
            self.assertEqual(data["total_queued"], 1)

    def test_queue_endpoint_without_task_id(self):
        """Test GET /queue (no task_id) returns queue statistics."""
        req = urllib.request.Request(f"{self.base_url}/queue")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertIn("queued", data)
            self.assertEqual(data["queued"], 1)

    def test_queue_position_missing_task(self):
        """Test GET /queue?task_id=nonexistent returns 404."""
        req = urllib.request.Request(f"{self.base_url}/queue?task_id=nonexistent")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(context.exception.code, 404)

    def test_queue_position_non_queued_task(self):
        """Test GET /queue?task_id=... for non-queued task returns None position."""
        # Transition task to claimed state
        self.scheduler.claim_task(self.task_id)

        req = urllib.request.Request(f"{self.base_url}/queue?task_id={self.task_id}")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertIsNone(data["queue_position"])
            self.assertIn("note", data)
            self.assertIn("claimed", data["note"])

    def test_queue_position_with_multiple_tasks(self):
        """Test queue position respects priority ordering."""
        # Create second task with higher priority
        task2_id = str(uuid.uuid4())
        self.scheduler.add_task(
            task_id=task2_id,
            kind="vision",
            payload={"source": "test2"},
            priority=20,  # Higher priority
        )

        # task_id should be position 2 (lower priority)
        req = urllib.request.Request(f"{self.base_url}/queue?task_id={self.task_id}")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["queue_position"], 2)

        # task2_id should be position 1 (higher priority)
        req = urllib.request.Request(f"{self.base_url}/queue?task_id={task2_id}")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["queue_position"], 1)

    def test_daemon_shutdown(self):
        """Test that daemon properly shuts down API server."""
        # Start a daemon with API
        from gtx_broker.daemon import SchedulerDaemon

        daemon = SchedulerDaemon(self.config)

        # Start the API on a different port to avoid collision
        api_port = self.port + 1
        self.assertTrue(daemon._start_status_api(api_port))

        # Verify server is running
        self.assertIsNotNone(daemon._api)
        self.assertIsNotNone(daemon._api.server)

        # Call shutdown directly (simulating finally block behavior)
        daemon._api.shutdown()

        # Verify server is stopped
        self.assertIsNone(daemon._api.server)

    def test_shutdown_releases_fixed_port_for_immediate_restart(self):
        """Shutdown must close the listening socket before a restart."""
        port = self.port + 1
        first = StatusAPI(self.scheduler, port=port)
        self.assertTrue(first.start())
        first_thread = Thread(target=first.run_forever, daemon=True)
        first_thread.start()
        time.sleep(0.1)
        first.shutdown()
        first_thread.join(timeout=2)
        self.assertFalse(first_thread.is_alive())

        second = StatusAPI(self.scheduler, port=port)
        self.assertTrue(second.start())
        second_thread = Thread(target=second.run_forever, daemon=True)
        second_thread.start()
        try:
            time.sleep(0.1)
        finally:
            second.shutdown()
            second_thread.join(timeout=2)
        self.assertFalse(second_thread.is_alive())

    def test_task_not_found_status(self):
        """Test GET /status/{task_id} returns 404 for missing task."""
        req = urllib.request.Request(f"{self.base_url}/status/nonexistent")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(context.exception.code, 404)


def test_status_api_port_configuration(monkeypatch):
    """The daemon accepts a valid port and falls back safely for bad values."""
    monkeypatch.setenv("GTX_STATUS_API_PORT", "12345")
    assert SchedulerDaemon._configured_status_api_port() == 12345

    monkeypatch.setenv("GTX_STATUS_API_PORT", "not-a-port")
    assert SchedulerDaemon._configured_status_api_port() == 11439

    monkeypatch.setenv("GTX_STATUS_API_PORT", "65536")
    assert SchedulerDaemon._configured_status_api_port() == 11439
