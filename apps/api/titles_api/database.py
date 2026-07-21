from collections.abc import Generator
import os
import hashlib
import sqlite3
import uuid
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(url: str | None = None) -> Engine:
    database_url = url or get_settings().database_url
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def upgrade_installed_database(database_url: str | None = None) -> None:
    """Advance an installed database through the immutable Alembic chain."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    resolved_url = database_url or get_settings().database_url
    project_root = Path(__file__).resolve().parents[3]
    config_path = project_root / "alembic.ini"
    config = Config(str(config_path)) if config_path.is_file() else Config()
    script_location = os.getenv("TITLES_ALEMBIC_SCRIPT_LOCATION") or str(project_root / "apps" / "api" / "alembic")
    config.set_main_option("script_location", script_location)
    config.set_main_option("sqlalchemy.url", resolved_url)

    app_support = os.getenv("TITLES_APP_SUPPORT")
    if resolved_url.startswith("sqlite:") and app_support:
        database = make_url(resolved_url).database
        if database and database not in {":memory:", ""} and "mode=memory" not in resolved_url:
            database_path = Path(database).expanduser().resolve()
            data_root = Path(app_support).expanduser().resolve()
            if database_path.parent != data_root:
                raise RuntimeError("packaged database must be inside TITLES_APP_SUPPORT")
            if database_path.exists():
                with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
                    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    current = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] if "alembic_version" in tables else None
                head = ScriptDirectory.from_config(config).get_current_head()
                if tables - {"alembic_version"} and current != head:
                    from .storage.installation_backup import InstallationBackupStore

                    backup_root = Path(os.getenv("TITLES_BACKUP_ROOT", str(data_root.parent / f"{data_root.name}-backups")))
                    InstallationBackupStore(backup_root).create(data_root, database_path.name)

    if resolved_url.startswith("sqlite:"):
        database = make_url(resolved_url).database
        if database and database not in {":memory:", ""} and "mode=memory" not in resolved_url:
            database_path = Path(database)
            if database_path.exists():
                from .storage.baselines import BaselineState, classify_database

                state = classify_database(database_path)
                if state is BaselineState.UNVERSIONED_0002:
                    command.stamp(config, "0002_generic_s3_sources")

    command.upgrade(config, "head")


def bootstrap_empty_local_database(database_url: str | None = None) -> bool:
    """Commit the storage contract only for a brand-new local SQLite install."""
    resolved_url = database_url or get_settings().database_url
    if not resolved_url.startswith("sqlite:"):
        return False
    database = make_url(resolved_url).database
    if not database or database in {":memory:", ""} or "mode=memory" in resolved_url:
        return False
    path = Path(database).resolve()
    with sqlite3.connect(path) as connection:
        workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        marker = connection.execute("SELECT marker FROM storage_cutover_marker WHERE id=1").fetchone()
        if workspace_count or marker is not None:
            return False
        binding_digest = hashlib.sha256(f"modelfiche-local:{path}".encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO storage_cutover_marker(id,marker,binding_digest,effect_epoch) VALUES(1,1,?,0)",
            (binding_digest,),
        )
        connection.commit()
        from .storage.gates import GateEvidence, StorageGateStore

        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        lease = f"local-bootstrap-{uuid.uuid4()}"
        evidence_digest = hashlib.sha256(f"{version}:1:{binding_digest}:1".encode("utf-8")).hexdigest()
        StorageGateStore(connection).verify_storage_contract(
            GateEvidence(
                run_lease_id=lease,
                alembic_head=version,
                marker=1,
                binding_digest=binding_digest,
                contract_version=1,
                evidence_digest=evidence_digest,
            ),
            expected_run_lease_id=lease,
        )
    return True


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
