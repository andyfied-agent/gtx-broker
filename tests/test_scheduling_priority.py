from pathlib import Path
import sys
import subprocess
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from gtx_broker.scheduler import (  # noqa: E402
    EpochManager,
    ScheduleWindow,
    Scheduler,
    SchedulerConfig,
)
from gtx_broker.scheduler.handlers import CodingHandler, HandlerResult
from gtx_broker.scheduler.workers import WorkerStatus


def make_scheduler(tmp_path):
    return Scheduler(SchedulerConfig(db_path=str(tmp_path / "tasks.db")))


def test_p40_tasks_are_selected_before_non_p40_immediate_work(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task("chat", "query", {}, "immediate", priority=100, idempotency_key="chat")
    scheduler.add_task("code", "coding", {}, "immediate", priority=1, idempotency_key="code")

    with patch.object(scheduler._policy, "get_current_window", return_value=ScheduleWindow.RESTRICTED):
        task = scheduler.get_next_task()

    assert task["id"] == "code"
    assert scheduler.select_worker_for_task(task) == "p40-coding"


def test_image_window_blocks_batch_until_images_are_drained(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task("image", "vision", {}, "vision", priority=1, idempotency_key="image")
    scheduler.add_task("batch", "coding", {}, "batch", priority=100, idempotency_key="batch")

    with patch.object(scheduler._policy, "get_current_window", return_value=ScheduleWindow.IMAGE_WINDOW):
        task = scheduler.get_next_task()
    assert task["id"] == "image"

    scheduler.cancel_task("image")
    with patch.object(scheduler._policy, "get_current_window", return_value=ScheduleWindow.BATCH_WINDOW):
        task = scheduler.get_next_task()
    assert task["id"] == "batch"
    assert scheduler.select_worker_for_task(task) == "slow-coder"


def test_immediate_coding_stays_on_p40(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task("code", "coding", {}, "immediate", priority=100, idempotency_key="code")

    task = scheduler.get_task("code")
    assert scheduler.select_worker_for_task(task) == "p40-coding"


def test_batch_coding_can_explicitly_use_p40(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task(
        "hard-code", "coding", {"worker_profile": "p40-coding"}, "batch",
        priority=100, idempotency_key="hard-code",
    )

    task = scheduler.get_task("hard-code")
    assert scheduler.select_worker_for_task(task) == "p40-coding"


def test_review_tag_does_not_fall_back_to_p40(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task(
        "review",
        "coding",
        {},
        "batch",
        priority=10,
        idempotency_key="review",
        review_tag=True,
    )
    task = scheduler.get_task("review")
    assert task["review_tag"] is True
    assert task["review_worker"] == "codex-review"
    assert scheduler.select_worker_for_task(task) is None


def test_review_prefers_codex_when_available(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler._worker_registry.update_status("codex-review", WorkerStatus.AVAILABLE)
    scheduler.add_task(
        "review-codex", "coding", {}, "batch", priority=10,
        idempotency_key="review-codex", review_tag=True,
    )

    task = scheduler.get_task("review-codex")
    assert scheduler.select_worker_for_task(task) == "codex-review"


def test_review_fails_over_to_air_when_codex_unavailable(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler._worker_registry.update_status("codex-review", WorkerStatus.UNAVAILABLE)
    scheduler._worker_registry.update_status("air-review", WorkerStatus.AVAILABLE)
    scheduler.add_task(
        "review-air", "coding", {}, "batch", priority=10,
        idempotency_key="review-air", review_tag=True,
    )

    task = scheduler.get_task("review-air")
    assert scheduler.select_worker_for_task(task) == "air-review"


def test_epoch_barrier_and_review_evidence_are_durable(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task("one", "coding", {}, "batch", idempotency_key="one")
    scheduler.add_task("two", "coding", {}, "batch", idempotency_key="two")
    epochs = EpochManager(scheduler)

    assert epochs.create_epoch("epoch-1", "nightly", ["one", "two"])
    assert not epochs.barrier_reached("epoch-1")

    for task_id in ("one", "two"):
        scheduler.claim_task(task_id)
        assert scheduler.start_task(task_id, "p40-coding")
        assert scheduler.complete_task(task_id, result={"commit": task_id})

    assert epochs.barrier_reached("epoch-1")
    assert epochs.trigger_review("epoch-1")
    assert epochs.record_review_result("epoch-1", {"findings": []})
    epoch = epochs.get_epoch("epoch-1")
    assert epoch["review_triggered"] == 1
    assert '"findings": []' in epoch["review_result"]


def test_nightly_coding_selects_slow_coder(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task(
        "nightly-coding",
        "coding",
        {},
        "batch",
        priority=100,
        idempotency_key="nightly-coding",
        schedule_type="nightly",
    )

    task = scheduler.get_task("nightly-coding")
    assert scheduler.select_worker_for_task(task) == "slow-coder"


def test_maintenance_coding_selects_slow_coder(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add_task(
        "maintenance-coding",
        "coding",
        {"mode": "maintenance"},
        "maintenance",
        priority=100,
        idempotency_key="maintenance-coding",
    )

    task = scheduler.get_task("maintenance-coding")
    assert scheduler.select_worker_for_task(task) == "slow-coder"


def test_p40_timeout_default_is_1800(monkeypatch, tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    task = {
        "kind": "coding",
        "payload": {
            "worktree_path": str(worktree),
            "executor_command": [sys.executable, "-c", "pass"],
            "require_commit": False,
            "allow_no_change": True,
        },
    }

    def run_with_timeout():
        with patch("gtx_broker.scheduler.handlers.subprocess.run", return_value=completed) as run:
            assert CodingHandler().execute(task) is HandlerResult.SUCCESS
            return run.call_args_list[1].kwargs["timeout"]

    monkeypatch.delenv("P40_CODING_TIMEOUT", raising=False)
    assert run_with_timeout() == 1800.0
