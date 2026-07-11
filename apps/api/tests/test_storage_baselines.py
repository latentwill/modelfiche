from __future__ import annotations

import sqlite3
from pathlib import Path
from alembic import command
from alembic.config import Config


import pytest

from titles_api.storage.baselines import (
    BaselineState,
    SchemaMismatch,
    classify_database,
    frozen_schema_sql,
    schema_fingerprint,
)


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _create_baseline(path: Path, name: str, *, version: str | None = None) -> None:
    with _connection(path) as connection:
        connection.executescript(frozen_schema_sql(name))
        if version is not None:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.execute("INSERT INTO alembic_version(version_num) VALUES(?)", (version,))
@pytest.mark.parametrize(
    ("schema_name", "version", "expected"),
    [
        ("unversioned", None, BaselineState.UNVERSIONED_0002),
        ("0001", "0001_initial", BaselineState.STAMPED_0001),
        ("0002", "0002_generic_s3_sources", BaselineState.STAMPED_0002),
    ],
)
def test_classify_database_accepts_only_exact_frozen_shapes(
    tmp_path: Path, schema_name: str, version: str | None, expected: BaselineState
) -> None:
    database_path = tmp_path / f"{schema_name}-{version or 'unversioned'}.sqlite3"
    _create_baseline(database_path, schema_name, version=version)

    assert classify_database(database_path) is expected


def test_empty_database_is_a_supported_baseline(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.sqlite3"
    with _connection(database_path):
        pass

    assert classify_database(database_path) is BaselineState.EMPTY


def test_shape_or_version_disagreement_is_rejected_without_mutating_source(tmp_path: Path) -> None:
    database_path = tmp_path / "mismatched.sqlite3"
    _create_baseline(database_path, "0002", version="0001_initial")
    with _connection(database_path) as connection:
        connection.execute("ALTER TABLE assets ADD COLUMN unexpected TEXT")
        before = schema_fingerprint(connection)

    with pytest.raises(SchemaMismatch, match="do not match"):
        classify_database(database_path)

    with _connection(database_path) as connection:
        assert schema_fingerprint(connection) == before
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001_initial",)


def test_frozen_schema_sql_is_deterministic_and_excludes_0002_source_columns() -> None:
    first = frozen_schema_sql("0001")

    assert first == frozen_schema_sql("0001")
    assert "credential_env_prefix" not in first
    assert "addressing_style" not in first
    assert "region" not in first
    assert "credential_env_prefix" in frozen_schema_sql("0002")


def _upgrade_to_revision(database_path: Path, revision: str) -> None:
    config = Config(str(Path(__file__).parents[3] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, revision)


def _upgrade_to_head(database_path: Path) -> None:
    _upgrade_to_revision(database_path, "head")


def test_classify_database_accepts_database_produced_by_0002_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated-0002.sqlite3"
    _create_baseline(database_path, "0001", version="0001_initial")

    _upgrade_to_revision(database_path, "0002_generic_s3_sources")

    assert classify_database(database_path) is BaselineState.STAMPED_0002


def test_frozen_migration_chain_creates_authoritative_storage_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "stamped-0001.sqlite3"
    _create_baseline(database_path, "0001", version="0001_initial")

    _upgrade_to_head(database_path)

    with _connection(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0003_flexible_image_storage",
        )
        for table in (
            "content_blobs",
            "storage_feature_gates",
            "storage_verification_receipts",
            "storage_cutover_marker",
            "storage_transfers",
            "checkpoint_revisions",
            "storage_migration_previews",
            "storage_migration_preview_items",
        ):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM storage_feature_gates WHERE name='storage_contract'"
        ).fetchone() == ("storage_contract",)
        reservation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(storage_reservations)")
        }
        assert {
            "transfer_id",
            "kind",
            "reserved_bytes",
            "generation",
            "lease_owner",
            "lease_expires_at",
        } <= reservation_columns
