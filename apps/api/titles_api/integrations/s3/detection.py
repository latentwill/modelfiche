from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Callable

from .models import Inventory


class PrefixKind(StrEnum):
    DATASET = "dataset"
    TRAINING_RUN = "training_run"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DetectionResult:
    kind: PrefixKind
    confidence: float
    signals: tuple[str, ...]
    warnings: tuple[str, ...]
    observed: dict[str, int]
    declared: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
_CONFIG_NAMES = {"config.json", "config.yaml", "config.yml", "train.yaml", "train.yml", "train_config.json"}
_SAMPLE_STEP = re.compile(r"(?:step|iteration|iter)[-_]?(\d+)", re.IGNORECASE)
_CHECKPOINT_STEP = re.compile(r"(?:step|checkpoint|ckpt)[-_]?(\d+)", re.IGNORECASE)


def _relative(prefix: str, key: str) -> str:
    return key[len(prefix.rstrip("/") + "/") :] if key.startswith(prefix.rstrip("/") + "/") else key


def detect_prefix(
    inventory: Inventory,
    read_small: Callable[[str], bytes] | None = None,
) -> DetectionResult:
    current = inventory.current()
    rel = [_relative(inventory.prefix, item.key) for item in current]
    names = {PurePosixPath(name).name.lower() for name in rel}
    image_stems = {str(PurePosixPath(name).with_suffix("")) for name in rel if PurePosixPath(name).suffix.lower() in _IMAGE_EXTENSIONS}
    text_stems = {str(PurePosixPath(name).with_suffix("")) for name in rel if PurePosixPath(name).suffix.lower() == ".txt"}
    checkpoints = [obj for obj, name in zip(current, rel) if PurePosixPath(name).suffix.lower() == ".safetensors"]
    sample_images = [obj for obj, name in zip(current, rel) if _is_sample(name)]
    config_objects = [obj for obj in current if PurePosixPath(obj.key).name.lower() in _CONFIG_NAMES]
    manifest_objects = [obj for obj in current if PurePosixPath(obj.key).name.lower() == "run_manifest.json"]

    dataset_signals: list[str] = []
    run_signals: list[str] = []
    if "captions.jsonl" in names:
        dataset_signals.append("captions.jsonl")
    paired = len(image_stems & text_stems)
    if paired:
        dataset_signals.append(f"{paired} image-caption pairs")
    for signal in ("caption_audit.md", "checksums.sha256"):
        if signal in names:
            dataset_signals.append(signal)
    if "run_manifest.json" in names:
        run_signals.append("run_manifest.json")
    if "run_state.json" in names:
        run_signals.append("run_state.json")
    if config_objects:
        run_signals.append(f"{len(config_objects)} trainer config(s)")
    if checkpoints:
        run_signals.append(f"{len(checkpoints)} checkpoint(s)")
    if sample_images:
        run_signals.append(f"{len(sample_images)} sample image(s)")

    declared: dict[str, int] = {}
    metadata: dict[str, Any] = {}
    warnings: list[str] = []
    if read_small and manifest_objects:
        try:
            manifest = json.loads(read_small(manifest_objects[0].key))
            metadata["manifest"] = manifest
            for field, label in (("checkpoints", "checkpoints"), ("samples", "samples")):
                value = manifest.get(field, [])
                declared[label] = len(value) if isinstance(value, list) else int(value or 0)
        except Exception as exc:
            warnings.append(f"run manifest could not be parsed: {exc}")

    observed = {
        "objects": len(current),
        "history_objects": inventory.history_count,
        "images": len(image_stems),
        "caption_pairs": paired,
        "checkpoints": len(checkpoints),
        "samples": len(sample_images),
    }
    for name in ("checkpoints", "samples"):
        if name in declared and declared[name] != observed[name]:
            warnings.append(
                f"manifest reports {declared[name]} {name}; observed inventory contains {observed[name]}"
            )
    image_only_dataset = bool(image_stems) and not (manifest_objects or checkpoints or sample_images or config_objects)
    if image_only_dataset and not paired:
        dataset_signals.append(f"{len(image_stems)} dataset image(s) without captions")
    dataset_score = min(1.0, 0.45 * ("captions.jsonl" in names) + 0.35 * bool(paired) + 0.4 * image_only_dataset + 0.1 * len(dataset_signals))
    run_score = min(1.0, 0.35 * bool(manifest_objects) + 0.3 * bool(checkpoints) + 0.2 * bool(sample_images) + 0.1 * bool(config_objects))
    if run_score >= dataset_score and run_score >= 0.35:
        kind, confidence, signals = PrefixKind.TRAINING_RUN, run_score, run_signals
    elif dataset_score >= 0.35:
        kind, confidence, signals = PrefixKind.DATASET, dataset_score, dataset_signals
    else:
        kind, confidence, signals = PrefixKind.UNKNOWN, max(run_score, dataset_score), dataset_signals + run_signals
    metadata["checkpoint_steps"] = sorted(filter(None, (_step(item.name, _CHECKPOINT_STEP) for item in checkpoints)))
    metadata["sample_steps"] = sorted(filter(None, (_step(item.name, _SAMPLE_STEP) for item in sample_images)))
    return DetectionResult(kind, confidence, tuple(signals), tuple(warnings), observed, declared, metadata)


def _is_sample(name: str) -> bool:
    path = PurePosixPath(name)
    return path.suffix.lower() in _IMAGE_EXTENSIONS and ("samples" in {part.lower() for part in path.parts} or bool(_SAMPLE_STEP.search(path.name)))


def _step(name: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(name)
    return int(match.group(1)) if match else None
