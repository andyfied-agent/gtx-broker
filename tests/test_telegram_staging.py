import json
from pathlib import Path

from PIL import Image

from gtx_broker.telegram_staging import stage_event


def _image(path: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(path, format="JPEG")


def test_stage_event_publishes_image_under_gtx_images(tmp_path):
    source = tmp_path / "hermes-cache" / "img_abc.jpg"
    source.parent.mkdir()
    _image(source)

    result = stage_event(
        {
            "source_path": str(source),
            "chat_id": 102287209,
            "message_id": 42,
            "user_id": 7,
            "caption": "receipt",
            "kind": "photo",
        },
        tmp_path / "gtx-images",
    )

    assert result.accepted is True
    assert result.status == "saved"
    assert result.idempotent_replay is False
    assert result.path is not None
    staged = Path(result.path)
    assert staged.parent.parent.name == "incoming"
    assert staged.name == "image.jpg"
    assert staged.read_bytes() == source.read_bytes()
    metadata = json.loads((staged.parent / "metadata.json").read_text())
    assert metadata["status"] == "saved"
    assert metadata["caption"] == "receipt"
    assert metadata["source_kind"] == "photo"


def test_stage_event_replay_returns_same_durable_path(tmp_path):
    source = tmp_path / "cached.jpg"
    _image(source)
    event = {"source_path": str(source), "chat_id": 1, "message_id": 2}
    storage_root = tmp_path / "gtx-images"

    first = stage_event(event, storage_root)
    second = stage_event(event, storage_root)

    assert first.accepted is True
    assert second.accepted is True
    assert second.idempotent_replay is True
    assert second.path == first.path
    assert len(list((storage_root / "incoming").iterdir())) == 1


def test_stage_event_rejects_missing_source_without_creating_task(tmp_path):
    result = stage_event(
        {"source_path": str(tmp_path / "missing.jpg"), "chat_id": 1, "message_id": 2},
        tmp_path / "gtx-images",
    )

    assert result.accepted is False
    assert result.status == "rejected"
    assert result.path is None
    assert not (tmp_path / "gtx-images" / "incoming").exists()
