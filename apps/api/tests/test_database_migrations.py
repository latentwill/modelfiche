from __future__ import annotations

import sqlite3
from pathlib import Path
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from titles_api import app as app_module
from titles_api import models
from titles_api.database import build_engine
from titles_api.database import upgrade_installed_database
from titles_api.storage.baselines import frozen_schema_sql



def test_upgrade_installed_database_adopts_exact_unversioned_baseline(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(frozen_schema_sql("unversioned"))

    upgrade_installed_database(f"sqlite:///{database_path}")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0015_backfill_legacy_krea_endpoints",)


def test_upgrade_installed_database_runs_frozen_alembic_chain(tmp_path: Path) -> None:
    database_path = tmp_path / "installed.sqlite3"

    upgrade_installed_database(f"sqlite:///{database_path}")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0015_backfill_legacy_krea_endpoints",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_feature_gates'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='training_metrics'"
        ).fetchone() == (1,)
        source_capability_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(source_capabilities)").fetchall()
        }
        assert "updated_at" in source_capability_columns
        content_blob_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(content_blobs)").fetchall()
        }
        assert {"created_at", "updated_at"} <= content_blob_columns
        assert "modified_at" in {row[1] for row in connection.execute("PRAGMA table_info(samples)")}
        assert "generated_at" in {row[1] for row in connection.execute("PRAGMA table_info(eval_outputs)")}
        assert connection.execute(
            "SELECT state FROM storage_feature_gates ORDER BY name"
        ).fetchall() == [("disabled",)] * 6
        assert connection.execute("SELECT marker FROM storage_cutover_marker WHERE id=1").fetchone() is None


def test_upgrade_installed_database_accepts_asset_timestamp_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0007.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            ("0007_asset_origin_timestamps",),
        )
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0015_backfill_legacy_krea_endpoints",
        )
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(eval_definitions)").fetchall()}
        assert columns["prompt_set_id"][3] == 0
        assert "inline_prompts" in columns
    engine = build_engine(database_url)
    with sessionmaker(bind=engine)() as session:
        assert session.scalar(select(models.CheckpointRevision)) is None
        assert session.scalar(select(models.LocalRoot)) is None
        assert session.scalar(select(models.StoragePolicy)) is None
        assert session.scalar(select(models.StoragePolicySnapshot)) is None


def test_upgrade_installed_database_accepts_checkpoint_revision_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0008.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            ("0008_checkpoint_revisions",),
        )
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0015_backfill_legacy_krea_endpoints",
        )


def test_upgrade_installed_database_accepts_live_inline_prompts_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0009.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE generation_queue_items")
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0009_inline_generation_prompts",))
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0015_backfill_legacy_krea_endpoints",)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_queue_items'").fetchone() == (1,)


def test_upgrade_installed_database_accepts_live_generation_queue_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0010.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE generation_queue_children")
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0010_generation_queue",))
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0015_backfill_legacy_krea_endpoints",)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_queue_children'").fetchone() == (1,)


def test_runtime_initialization_migrates_but_does_not_seed_before_cutover_marker(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("TITLES_DATABASE_URL", database_url)
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(tmp_path / "assets"))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("TITLES_EXPORT_ROOT", str(tmp_path / "exports"))
    app_module.get_settings.cache_clear()
    test_engine = build_engine(database_url)
    test_session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(app_module, "engine", test_engine)
    monkeypatch.setattr(app_module, "SessionLocal", test_session)

    assert app_module.initialize_database() is False

    with test_session() as session:
        assert session.scalar(select(models.Workspace)) is None
        assert session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_cutover_marker'")
        ).fetchone() == (1,)
    app_module.get_settings.cache_clear()
