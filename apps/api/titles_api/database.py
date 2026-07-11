from collections.abc import Generator
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

    resolved_url = database_url or get_settings().database_url
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "apps" / "api" / "alembic"))
    config.set_main_option("sqlalchemy.url", resolved_url)

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


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
