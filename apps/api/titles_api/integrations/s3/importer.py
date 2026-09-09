from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from .browser import S3Browser
from .detection import DetectionResult, PrefixKind, detect_prefix
from .models import Inventory, ObjectInfo
from .reconciliation import ChangeKind, StoredObject, reconcile


class ImportSink(Protocol):
    """Domain persistence hook implemented by the API's repository layer."""

    def stored_objects(self, source_id: str, prefix: str) -> list[StoredObject]: ...

    def upsert_remote_object(self, source_id: str, item: ObjectInfo, *, category: str) -> str: ...

    def mark_remote_missing(self, source_id: str, key: str) -> None: ...

    def save_source_snapshot(self, source_id: str, key: str, body: bytes) -> None: ...

    def finalize_import(self, context: "ImportContext", result: "ImportResult") -> None: ...


@dataclass(frozen=True, slots=True)
class ImportContext:
    source_id: str
    project_id: str
    prefix: str
    requested_by_profile_id: str | None = None
    hydrate_dataset_images: bool = True


@dataclass(frozen=True, slots=True)
class ImportResult:
    detection: DetectionResult
    indexed: int
    changed: int
    unchanged: int
    remote_missing: int
    small_sources_saved: int
    warnings: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "detection": {
                "kind": self.detection.kind.value,
                "confidence": self.detection.confidence,
                "signals": list(self.detection.signals),
                "warnings": list(self.detection.warnings),
                "observed": self.detection.observed,
                "declared": self.detection.declared,
                "metadata": self.detection.metadata,
            },
            "indexed": self.indexed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "remote_missing": self.remote_missing,
            "small_sources_saved": self.small_sources_saved,
            "warnings": list(self.warnings),
        }


_SMALL_SOURCE_NAMES = {
    "aitk_db.db",
    "captions.jsonl",
    "config.json",
    "config.yaml",
    "config.yml",
    "checksums.sha256",
    "loss_log.db",
    "log.txt",
    "metrics.jsonl",
    "run_manifest.json",
    "run_state.json",
    "train.yaml",
    "train.yml",
    "train.log",
    "training.log",
    "training_args.json",
}

_SAMPLE_DIRECTORY_NAMES = frozenset({
    "eval-sample",
    "eval-samples",
    "eval_sample",
    "eval_samples",
    "evaluation-sample",
    "evaluation-samples",
    "evaluation_sample",
    "evaluation_samples",
    "sample",
    "samples",
})


class S3Importer:
    def __init__(self, browser: S3Browser, sink: ImportSink):
        self.browser = browser
        self.sink = sink

    def preview(self, prefix: str) -> tuple[Inventory, DetectionResult]:
        inventory = self.browser.inventory(prefix)
        return inventory, detect_prefix(inventory, self.browser.read_small)

    def run(self, context: ImportContext) -> ImportResult:
        inventory, detection = self.preview(context.prefix)
        if detection.kind == PrefixKind.UNKNOWN:
            raise ValueError("selected prefix is not a recognized dataset or training run")
        current = inventory.current()
        changes = reconcile(current, self.sink.stored_objects(context.source_id, inventory.prefix))
        indexed = changed = unchanged = missing = sources = 0
        warnings = list(detection.warnings)
        for change in changes:
            try:
                if change.kind == ChangeKind.REMOTE_MISSING:
                    self.sink.mark_remote_missing(context.source_id, change.key)
                    missing += 1
                    continue
                if change.kind == ChangeKind.UNCHANGED:
                    assert change.remote is not None
                    category = classify_object(change.remote, detection.kind)
                    self.sink.upsert_remote_object(context.source_id, change.remote, category=category)
                    if _should_read_small(change.remote, detection.kind):
                        body = self.browser.read_small(change.remote.key)
                        self.sink.save_source_snapshot(context.source_id, change.remote.key, body)
                        sources += 1
                    unchanged += 1
                    continue
                assert change.remote is not None
                category = classify_object(change.remote, detection.kind)
                self.sink.upsert_remote_object(context.source_id, change.remote, category=category)
                indexed += 1
                changed += change.kind == ChangeKind.CHANGED
                if _should_read_small(change.remote, detection.kind):
                    body = self.browser.read_small(change.remote.key)
                    self.sink.save_source_snapshot(context.source_id, change.remote.key, body)
                    sources += 1
            except Exception as exc:
                warnings.append(f"{change.key}: {exc}")
        result = ImportResult(detection, indexed, changed, unchanged, missing, sources, tuple(warnings))
        self.sink.finalize_import(context, result)
        return result


def _should_read_small(item: ObjectInfo, kind: PrefixKind) -> bool:
    name = PurePosixPath(item.key).name.lower()
    return item.size <= 2 * 1024 * 1024 and (
        name in _SMALL_SOURCE_NAMES
        or name.startswith("events.out.tfevents.")
        or (kind == PrefixKind.DATASET and PurePosixPath(name).suffix == ".txt")
    )


def classify_object(item: ObjectInfo, kind: PrefixKind) -> str:
    path = PurePosixPath(item.key)
    ext = path.suffix.lower()
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if ext == ".safetensors":
        return "checkpoint"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}:
        return "sample" if kind == PrefixKind.TRAINING_RUN and parts & _SAMPLE_DIRECTORY_NAMES else "dataset_image"
    if name in {"run_manifest.json", "run_state.json"}:
        return "run_metadata"
    if name in _SMALL_SOURCE_NAMES or name.startswith("events.out.tfevents."):
        return "source_metadata"
    if kind == PrefixKind.DATASET and ext == ".txt":
        return "caption"
    if ext in {".log", ".out"} or name == "log.txt":
        return "log"
    if ext in {".pt", ".sqlite", ".sqlite3", ".wal"} or name.endswith(".pid"):
        return "sidecar"
    return "artifact"
