"""Integration tests for status API."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread

import requests
import uuid

from gtx_broker.scheduler import Scheduler, SchedulerConfig
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

        # Create test task
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
        response = requests.get(f"{self.base_url}/status/{self.task_id}", timeout=5)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["task_id"], self.task_id)
        self.assertEqual(data["state"], "queued")

    def test_cancel_queued_task(self):
        """Test POST /cancel works on queued tasks."""
        payload = {"task_id": self.task_id}
        response = requests.post(
            f"{self.base_url}/cancel",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

    def test_uses_scheduler_database(self):
        """Verify API uses scheduler's database path."""
        self.assertEqual(self.scheduler.config.db_path, str(self.temp_db))


if __name__ == "__main__":
    unittest.main()
