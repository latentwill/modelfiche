from __future__ import annotations

import sqlite3
from pathlib import Path
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from titles_api import app as app_module
from titles_api import models
from titles_api.database import bootstrap_empty_local_database, build_engine
from titles_api.database import upgrade_installed_database
from titles_api.storage.baselines import frozen_schema_sql





def test_packaged_upgrade_creates_verified_pre_migration_backup(tmp_path: Path, monkeypatch) -> None:
    support = tmp_path / "Modelfiche"
    support.mkdir()
    database_path = support / "modelfiche.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(frozen_schema_sql("unversioned"))
    (support / "assets").mkdir()
    (support / "assets" / "original.bin").write_bytes(b"original")
    backup_root = tmp_path / "backups"
    monkeypatch.setenv("TITLES_APP_SUPPORT", str(support))
    monkeypatch.setenv("TITLES_BACKUP_ROOT", str(backup_root))

    upgrade_installed_database(f"sqlite:///{database_path}")

    archives = list(backup_root.glob("modelfiche-*.tar.gz"))
    assert len(archives) == 1
    from titles_api.storage.installation_backup import InstallationBackupStore

    assert len(InstallationBackupStore(backup_root).verify(archives[0])) == 64


def test_upgrade_installed_database_runs_frozen_alembic_chain(tmp_path: Path) -> None:
    database_path = tmp_path / "installed.sqlite3"

    upgrade_installed_database(f"sqlite:///{database_path}")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_feature_gates'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='training_metrics'"
        ).fetchone() == (1,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "wandb_ingest_credentials",
            "training_launches",
            "run_metric_summaries",
            "run_events",
            "run_uploads",
            "run_artifact_handoffs",
        } <= tables
        credential_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(wandb_ingest_credentials)"
            ).fetchall()
        }
        assert "workspace_id" not in credential_columns
        assert "project_id" not in credential_columns
        assert {
            "wandb_run_id",
            "wandb_project",
            "current_step",
            "last_event_at",
            "exit_code",
        } <= {
            row[1]
            for row in connection.execute("PRAGMA table_info(training_runs)").fetchall()
        }
        assert {"value_text", "value_type", "batch_key"} <= {
            row[1]
            for row in connection.execute("PRAGMA table_info(training_metrics)").fetchall()
        }
        assert {"parent_version_id", "lineage_kind", "content_digest"} <= {
            row[1]
            for row in connection.execute("PRAGMA table_info(dataset_versions)").fetchall()
        }
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
        assert {"artifact_type", "artifact_format", "method", "compatibility"} <= {
            row[1] for row in connection.execute("PRAGMA table_info(model_versions)")
        }
        assert connection.execute(
            "SELECT state FROM storage_feature_gates ORDER BY name"
        ).fetchall() == [("disabled",)] * 6
        assert connection.execute("SELECT marker FROM storage_cutover_marker WHERE id=1").fetchone() is None


def test_fresh_local_database_bootstraps_storage_contract_once(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-local.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)

    assert bootstrap_empty_local_database(database_url) is True
    assert bootstrap_empty_local_database(database_url) is False
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT marker FROM storage_cutover_marker WHERE id=1").fetchone() == (1,)
        assert connection.execute(
            "SELECT state, verification_receipt_id FROM storage_feature_gates WHERE name='storage_contract'"
        ).fetchone()[0] == "ready"


def test_upgrade_installed_database_accepts_asset_timestamp_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0007.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(eval_definitions)").fetchall()}
        assert columns["prompt_set_id"][3] == 0
        assert "inline_prompts" in columns
    engine = build_engine(database_url)
    with sessionmaker(bind=engine)() as session:
        assert session.scalar(select(models.CheckpointRevision)) is None
        assert session.scalar(select(models.LocalRoot)) is None
        assert session.scalar(select(models.StoragePolicy)) is None
        assert session.scalar(select(models.StoragePolicySnapshot)) is None




def test_upgrade_installed_database_accepts_live_inline_prompts_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0009.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_queue_items'").fetchone() == (1,)


def test_upgrade_installed_database_accepts_live_generation_queue_head(tmp_path: Path) -> None:
    database_path = tmp_path / "installed-0010.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_installed_database(database_url)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_queue_children'").fetchone() == (1,)


def test_runtime_initialization_bootstraps_fresh_local_install(
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

    assert app_module.initialize_database() is True

    with test_session() as session:
        assert session.scalar(select(models.Workspace)) is not None
        assert session.scalar(select(models.UserProfile)) is not None
        assert session.execute(
            text("SELECT marker FROM storage_cutover_marker WHERE id=1")
        ).fetchone() == (1,)
    app_module.get_settings.cache_clear()
