from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models


MAX_STEP = 2_147_483_647
MAX_METRIC_NAME = 120
MAX_TEXT_VALUE = 16_384


class MetricValidationError(ValueError):
    pass


class MetricBatchConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MetricInput:
    step: int
    name: str
    value: float | int | str | bool | None
    wall_time: float | None = None
    source_key: str | None = None


@dataclass(frozen=True, slots=True)
class MetricIngestResult:
    inserted: int
    corrected: int
    unchanged: int
    duplicate_batch: bool
    event_sequence: int | None


def _typed_value(value: float | int | str | bool | None) -> tuple[str, float | None, str | None]:
    if value is None:
        return "null", None, None
    if isinstance(value, bool):
        return "text", None, "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise MetricValidationError("numeric metric values must be finite")
        return "number", number, None
    if isinstance(value, str):
        if len(value) > MAX_TEXT_VALUE:
            raise MetricValidationError(f"text metric values may not exceed {MAX_TEXT_VALUE} characters")
        return "text", None, value
    raise MetricValidationError(f"unsupported metric value type: {type(value).__name__}")


def _validate_point(point: MetricInput) -> tuple[str, float | None, str | None]:
    if isinstance(point.step, bool) or not isinstance(point.step, int) or not 0 <= point.step <= MAX_STEP:
        raise MetricValidationError(f"metric step must be between 0 and {MAX_STEP}")
    if not isinstance(point.name, str) or not point.name or len(point.name) > MAX_METRIC_NAME:
        raise MetricValidationError(f"metric name must contain 1-{MAX_METRIC_NAME} characters")
    if point.wall_time is not None and not math.isfinite(point.wall_time):
        raise MetricValidationError("metric wall_time must be finite")
    return _typed_value(point.value)


def _batch_digest(points: list[MetricInput], committed_step: int | None) -> str:
    payload = {
        "committed_step": committed_step,
        "metrics": [
            {
                "step": point.step,
                "name": point.name,
                "value": point.value,
                "wall_time": point.wall_time,
                "source_key": point.source_key,
            }
            for point in points
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def append_run_event(
    session: Session,
    run: models.TrainingRun,
    *,
    type: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    step: int | None = None,
    occurred_at: datetime | None = None,
) -> models.RunEvent:
    existing = session.scalar(
        select(models.RunEvent).where(models.RunEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    last_sequence = session.scalar(
        select(func.max(models.RunEvent.sequence)).where(models.RunEvent.run_id == run.id)
    )
    event = models.RunEvent(
        run_id=run.id,
        sequence=(last_sequence or 0) + 1,
        type=type,
        step=step,
        payload=payload or {},
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or models.utcnow(),
    )
    session.add(event)
    run.last_event_at = event.occurred_at
    return event


def _refresh_summary(session: Session, run_id: str, name: str) -> None:
    latest = session.scalar(
        select(models.TrainingMetric)
        .where(
            models.TrainingMetric.run_id == run_id,
            models.TrainingMetric.name == name,
        )
        .order_by(models.TrainingMetric.step.desc())
        .limit(1)
    )
    if latest is None:
        return
    minimum, maximum = session.execute(
        select(func.min(models.TrainingMetric.value), func.max(models.TrainingMetric.value)).where(
            models.TrainingMetric.run_id == run_id,
            models.TrainingMetric.name == name,
            models.TrainingMetric.value_type == "number",
        )
    ).one()
    summary = session.scalar(
        select(models.RunMetricSummary).where(
            models.RunMetricSummary.run_id == run_id,
            models.RunMetricSummary.name == name,
        )
    )
    if summary is None:
        summary = models.RunMetricSummary(run_id=run_id, name=name)
        session.add(summary)
    summary.last_step = latest.step
    summary.last_value = latest.value
    summary.last_value_text = latest.value_text
    summary.value_type = latest.value_type
    summary.minimum = minimum
    summary.maximum = maximum
    summary.updated_at = models.utcnow()


def ingest_metric_batch(
    session: Session,
    run_id: str,
    points: Iterable[MetricInput],
    *,
    batch_key: str,
    committed_step: int | None = None,
    occurred_at: datetime | None = None,
) -> MetricIngestResult:
    materialized = list(points)
    if not batch_key or len(batch_key) > 128:
        raise MetricValidationError("batch_key must contain 1-128 characters")
    if committed_step is not None and (
        isinstance(committed_step, bool)
        or not isinstance(committed_step, int)
        or not 0 <= committed_step <= MAX_STEP
    ):
        raise MetricValidationError(f"committed_step must be between 0 and {MAX_STEP}")
    typed = [_validate_point(point) for point in materialized]
    digest = _batch_digest(materialized, committed_step)
    idempotency_key = f"run.metrics:{run_id}:{batch_key}"

    existing_event = session.scalar(
        select(models.RunEvent).where(models.RunEvent.idempotency_key == idempotency_key)
    )
    if existing_event is not None:
        if existing_event.payload.get("digest") != digest:
            raise MetricBatchConflict("metric batch key was reused with different content")
        return MetricIngestResult(0, 0, len(materialized), True, existing_event.sequence)

    run = session.get(models.TrainingRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError(f"training run {run_id!r} does not exist")

    inserted = corrected = unchanged = 0
    affected_names: set[str] = set()
    correction_keys: list[str] = []
    for point, (value_type, numeric_value, text_value) in zip(materialized, typed, strict=True):
        metric = session.scalar(
            select(models.TrainingMetric).where(
                models.TrainingMetric.run_id == run.id,
                models.TrainingMetric.step == point.step,
                models.TrainingMetric.name == point.name,
            )
        )
        desired = (value_type, numeric_value, text_value, point.wall_time, point.source_key)
        if metric is None:
            session.add(
                models.TrainingMetric(
                    run_id=run.id,
                    step=point.step,
                    name=point.name,
                    value=numeric_value,
                    value_text=text_value,
                    value_type=value_type,
                    wall_time=point.wall_time,
                    source_key=point.source_key,
                    ingested_at=occurred_at or models.utcnow(),
                    batch_key=batch_key,
                )
            )
            inserted += 1
            affected_names.add(point.name)
            continue
        current = (metric.value_type, metric.value, metric.value_text, metric.wall_time, metric.source_key)
        if current == desired:
            unchanged += 1
            continue
        metric.value_type = value_type
        metric.value = numeric_value
        metric.value_text = text_value
        metric.wall_time = point.wall_time
        metric.source_key = point.source_key
        metric.ingested_at = occurred_at or models.utcnow()
        metric.batch_key = batch_key
        corrected += 1
        affected_names.add(point.name)
        correction_keys.append(f"{point.step}:{point.name}")

    session.flush()
    for name in affected_names:
        _refresh_summary(session, run.id, name)

    max_step = max(
        [point.step for point in materialized]
        + ([committed_step] if committed_step is not None else [])
        + ([run.current_step] if run.current_step is not None else [])
    ) if materialized or committed_step is not None or run.current_step is not None else None
    run.current_step = max_step
    event = append_run_event(
        session,
        run,
        type="run.metrics.committed",
        idempotency_key=idempotency_key,
        step=committed_step if committed_step is not None else max_step,
        occurred_at=occurred_at,
        payload={
            "digest": digest,
            "inserted": inserted,
            "corrected": corrected,
            "unchanged": unchanged,
            "corrections": correction_keys,
            "metric_names": sorted({point.name for point in materialized}),
        },
    )
    session.flush()
    return MetricIngestResult(inserted, corrected, unchanged, False, event.sequence)
