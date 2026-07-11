from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import FrozenInstanceError

import pytest

from titles_api.integrations.s3.metrics import MetricPoint, parse_loss_log_db, parse_training_log


def _loss_log_db(
    *,
    steps: list[tuple[object, object]],
    metrics: list[tuple[object, object, object, object]],
) -> bytes:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE steps (step INTEGER, wall_time REAL);
            CREATE TABLE metrics (
                step INTEGER,
                key TEXT,
                value_real REAL,
                value_text TEXT
            );
            """
        )
        connection.executemany("INSERT INTO steps VALUES (?, ?)", steps)
        connection.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?)", metrics)
        return connection.serialize()
    finally:
        connection.close()


def test_parse_loss_log_db_returns_numeric_metric_points_with_wall_time() -> None:
    body = _loss_log_db(
        steps=[(2, 123.5), (1, None)],
        metrics=[
            (2, "loss", 0.5, None),
            (1, "loss", 1.0, None),
            (1, "accuracy", 0.75, None),
            (1, "caption", None, "text-only"),
        ],
    )

    points = parse_loss_log_db(body)

    assert points == [
        MetricPoint(step=1, name="accuracy", value=0.75, wall_time=None),
        MetricPoint(step=1, name="loss", value=1.0, wall_time=None),
        MetricPoint(step=2, name="loss", value=0.5, wall_time=123.5),
    ]
    with pytest.raises(FrozenInstanceError):
        points[0].step = 2  # type: ignore[misc]



def test_parse_loss_log_db_accepts_wal_mode_and_normalizes_ai_toolkit_loss() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as file:
        connection = sqlite3.connect(file.name)
        connection.execute("PRAGMA journal_mode=wal")
        connection.executescript(
            """
            CREATE TABLE steps (step INTEGER PRIMARY KEY, wall_time REAL NOT NULL);
            CREATE TABLE metrics (step INTEGER NOT NULL, key TEXT NOT NULL, value_real REAL, value_text TEXT);
            INSERT INTO steps VALUES (1, 10.0);
            INSERT INTO metrics VALUES (1, 'loss/loss', 0.5, NULL);
            """
        )
        connection.commit()
        connection.close()
        file.seek(0)
        body = file.read()

    assert parse_loss_log_db(body) == [MetricPoint(step=1, name="loss", value=0.5, wall_time=10.0)]

def test_parse_loss_log_db_returns_empty_for_malformed_bytes() -> None:
    assert parse_loss_log_db(b"not a sqlite database") == []


def test_parse_loss_log_db_returns_empty_for_unsupported_schema() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE metrics (step INTEGER, key TEXT)")
        body = connection.serialize()
    finally:
        connection.close()

    assert parse_loss_log_db(body) == []


def test_parse_loss_log_db_skips_nonnumeric_and_nonfinite_values() -> None:
    body = _loss_log_db(
        steps=[(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0), (4, 5.0)],
        metrics=[
            (0, "valid", 2.5, None),
            (1, "none", None, "not numeric"),
            (2, "text", "not numeric", None),
            (3, "nan", float("nan"), None),
            (4, "infinity", float("inf"), None),
        ],
    )

    assert parse_loss_log_db(body) == [
        MetricPoint(step=0, name="valid", value=2.5, wall_time=1.0)
    ]


def test_parse_loss_log_db_sorts_by_name_then_step() -> None:
    body = _loss_log_db(
        steps=[(3, 3.0), (1, 1.0), (2, 2.0)],
        metrics=[
            (3, "zeta", 3.0, None),
            (1, "alpha", 1.0, None),
            (2, "zeta", 2.0, None),
            (2, "alpha", 2.0, None),
        ],
    )

    assert parse_loss_log_db(body) == [
        MetricPoint(step=1, name="alpha", value=1.0, wall_time=1.0),
        MetricPoint(step=2, name="alpha", value=2.0, wall_time=2.0),
        MetricPoint(step=2, name="zeta", value=2.0, wall_time=2.0),
        MetricPoint(step=3, name="zeta", value=3.0, wall_time=3.0),
    ]


def test_parse_loss_log_db_limits_result_to_100_000_rows() -> None:
    row_count = 100_005
    body = _loss_log_db(
        steps=[(step, float(step)) for step in range(row_count)],
        metrics=[(step, "loss", float(step), None) for step in range(row_count)],
    )

    points = parse_loss_log_db(body)

    assert len(points) == 100_000
    assert points[0] == MetricPoint(step=0, name="loss", value=0.0, wall_time=0.0)
    assert points[-1] == MetricPoint(
        step=99_999,
        name="loss",
        value=99_999.0,
        wall_time=99_999.0,
    )


def test_parse_training_log_extracts_json_loss_points() -> None:
    body = b'{"step": 1, "loss": 1.25}\n{"step": 2, "loss": 0.75}\n'

    assert parse_training_log(body) == [
        MetricPoint(step=1, name="loss", value=1.25),
        MetricPoint(step=2, name="loss", value=0.75),
    ]
