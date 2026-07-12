from datetime import datetime, timezone

from titles_api.integrations.s3.detection import PrefixKind, detect_prefix
from titles_api.integrations.s3.models import Inventory, ObjectInfo
from titles_api.integrations.s3.reconciliation import ChangeKind, StoredObject, reconcile


def obj(key: str, size: int = 10, etag: str = "a") -> ObjectInfo:
    return ObjectInfo(key, size, etag, datetime.now(timezone.utc))


def test_dataset_detection_pairs_images_and_captions():
    prefix = "title-lora/datasets/test/"
    inventory = Inventory(prefix, [obj(prefix + "captions.jsonl"), obj(prefix + "one.png"), obj(prefix + "one.txt")])
    result = detect_prefix(inventory)
    assert result.kind == PrefixKind.DATASET
    assert result.observed["caption_pairs"] == 1


def test_dataset_detection_accepts_image_only_source_without_run_signals():
    prefix = "title-lora/datasets/no-captions/"
    inventory = Inventory(prefix, [obj(prefix + "one.png"), obj(prefix + "two.jpg")])

    result = detect_prefix(inventory)

    assert result.kind == PrefixKind.DATASET
    assert result.observed["images"] == 2
    assert result.observed["caption_pairs"] == 0
    assert result.signals == ("2 dataset image(s) without captions",)


def test_run_detection_warns_when_manifest_inventory_is_stale():
    prefix = "title-lora/projects/p/runs/r/"
    inventory = Inventory(
        prefix,
        [obj(prefix + "run_manifest.json"), obj(prefix + "checkpoints/step-2000.safetensors"), obj(prefix + "samples/step-2000.png")],
    )
    result = detect_prefix(inventory, lambda _key: b'{"checkpoints": [], "samples": []}')
    assert result.kind == PrefixKind.TRAINING_RUN
    assert result.observed["checkpoints"] == 1
    assert any("manifest reports 0 checkpoints" in warning for warning in result.warnings)


def test_reconciliation_never_deletes_missing_remote_records():
    changes = reconcile([obj("root/changed", etag="new"), obj("root/new")], [StoredObject("root/changed", "old", 10), StoredObject("root/gone", "x", 10)])
    assert {change.key: change.kind for change in changes} == {
        "root/changed": ChangeKind.CHANGED,
        "root/gone": ChangeKind.REMOTE_MISSING,
        "root/new": ChangeKind.NEW,
    }
