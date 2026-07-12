from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from typing import Any

_MAX_METRIC_POINTS = 100_000


@dataclass(frozen=True, slots=True)
class MetricPoint:
    step: int
    name: str
    value: float
    wall_time: float | None = None


def parse_loss_log_db(body: bytes) -> list[MetricPoint]:
    """Parse a small ai-toolkit SQLite snapshot without retaining it locally.

    The temporary file is required for WAL-mode databases, which SQLite cannot
    reliably deserialize from a standalone main-file byte string. It is removed
    as soon as parsing completes.
    """
    if not isinstance(body, bytes):
        return []

    try:
        with tempfile.NamedTemporaryFile(prefix="titles-metric-", suffix=".db") as snapshot:
            snapshot.write(body)
            snapshot.flush()
            with closing(sqlite3.connect(f"file:{snapshot.name}?mode=ro", uri=True)) as connection:
                rows = connection.execute(
                    """
                    SELECT metrics.step, metrics.key, metrics.value_real, steps.wall_time
                    FROM metrics
                    LEFT JOIN steps ON steps.step = metrics.step
                    LIMIT ?
                    """,
                    (_MAX_METRIC_POINTS,),
                )
                points: list[MetricPoint] = []
                for raw_step, raw_name, raw_value, raw_wall_time in rows:
                    step = _coerce_step(raw_step)
                    name = _coerce_name(raw_name)
                    value = _coerce_finite_float(raw_value)
                    if step is None or name is None or value is None:
                        continue
                    points.append(MetricPoint(step, name, value, _coerce_wall_time(raw_wall_time)))
                points.sort(key=lambda point: (point.name, point.step))
                return points
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return []

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_KEY_VALUE = re.compile(rf"(?P<key>[A-Za-z][A-Za-z0-9_.\-/]*)\s*[=:]\s*(?P<value>{_NUMBER})")
_STEP_KEYS = {"step", "global_step", "iteration", "iter"}


def parse_training_log(body: bytes) -> list[MetricPoint]:
    if not isinstance(body, bytes):
        return []
    points: list[MetricPoint] = []
    for line in body.decode("utf-8", "replace").splitlines():
        points.extend(_parse_json_log_line(line))
        points.extend(_parse_text_log_line(line))
        if len(points) >= _MAX_METRIC_POINTS:
            break
    points.sort(key=lambda point: (point.name, point.step))
    return points[:_MAX_METRIC_POINTS]


def parse_tensorboard_event(body: bytes) -> list[MetricPoint]:
    """Read scalar summaries from one TensorBoard event stream."""
    if not isinstance(body, bytes):
        return []
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        from tensorboard.util import tensor_util

        with tempfile.NamedTemporaryFile(prefix="titles-tfevents-") as snapshot:
            snapshot.write(body)
            snapshot.flush()
            accumulator = EventAccumulator(snapshot.name, size_guidance={"scalars": 0, "tensors": 0})
            accumulator.Reload()
            points: list[MetricPoint] = []
            tags = accumulator.Tags()
            for tag in tags.get("scalars", []):
                name = _canonical_metric_name(tag)
                for event in accumulator.Scalars(tag):
                    value = _coerce_finite_float(event.value)
                    if value is not None:
                        points.append(MetricPoint(int(event.step), name, value, float(event.wall_time)))
            for tag in tags.get("tensors", []):
                name = _canonical_metric_name(tag)
                for event in accumulator.Tensors(tag):
                    values = tensor_util.make_ndarray(event.tensor_proto).reshape(-1)
                    value = _coerce_finite_float(values[0]) if len(values) else None
                    if value is not None:
                        points.append(MetricPoint(int(event.step), name, value, float(event.wall_time)))
            points.sort(key=lambda point: (point.name, point.step))
            return points[:_MAX_METRIC_POINTS]
    except (ImportError, OSError, TypeError, ValueError, OverflowError):
        return []


def _canonical_metric_name(name: str) -> str:
    lowered = name.lower().strip()
    if "learning_rate" in lowered or lowered.endswith("/lr") or lowered == "lr":
        return "learning_rate"
    if "loss" in lowered:
        return "loss"
    return name


def _parse_json_log_line(line: str) -> list[MetricPoint]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, dict):
        return []
    raw_step = next((value.get(key) for key in _STEP_KEYS if value.get(key) is not None), None)
    step = _coerce_step(raw_step)
    if step is None:
        return []
    wall_time = _coerce_wall_time(value.get("wall_time"))
    points: list[MetricPoint] = []
    for name, raw_value in value.items():
        if name in _STEP_KEYS or name == "wall_time":
            continue
        metric_value = _coerce_finite_float(raw_value)
        if metric_value is not None:
            points.append(MetricPoint(step, str(name), metric_value, wall_time))
    return points


def _parse_text_log_line(line: str) -> list[MetricPoint]:
    pairs = [(match.group("key"), match.group("value")) for match in _KEY_VALUE.finditer(line)]
    step = next((_coerce_step(value) for key, value in pairs if key.lower() in _STEP_KEYS), None)
    if step is None:
        progress = re.search(r"(?P<step>\d+)\s*/\s*(?P<total>\d+)", line)
        step = _coerce_step(progress.group("step")) if progress else None
    if step is None:
        return []
    return [
        MetricPoint(step, "learning_rate" if key.lower() == "lr" else key, float(value))
        for key, value in pairs
        if key.lower() not in _STEP_KEYS and ("loss" in key.lower() or key.lower() in {"lr", "learning_rate"})
    ]


def _coerce_step(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    name = value if isinstance(value, str) else str(value)
    return "loss" if name == "loss/loss" else name


def _coerce_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _coerce_wall_time(value: Any) -> float | None:
    return _coerce_finite_float(value)
