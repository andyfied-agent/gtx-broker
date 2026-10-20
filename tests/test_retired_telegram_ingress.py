import json
import subprocess
import sys

from gtx_broker.telegram_ingress import TelegramImageIngress


def test_cli_rejects_without_creating_scheduler_state(tmp_path):
    db_path = tmp_path / "metadata" / "tasks.db"
    event = {"source_path": str(tmp_path / "image.jpg")}

    completed = subprocess.run(
        [sys.executable, "-m", "gtx_broker.telegram_ingress", "--json-stdin"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
        env={**__import__("os").environ, "GTX_SCHEDULER_DB": str(db_path)},
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "accepted": False,
        "error": TelegramImageIngress.DISABLED_REASON,
        "path": None,
        "quality": None,
        "status": "rejected",
        "task_id": None,
    }
    assert not db_path.exists()
