import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from titles_api import app as app_module, models
from titles_api.database import Base, get_db
from titles_api.settings import get_settings
from titles_api.security_middleware import LocalLaunchContract
from titles_api.storage.gates import GateEvidence, StorageGateStore


def _install_model_test_foundation(engine) -> None:
    connection = engine.raw_connection()
    raw = connection.driver_connection
    try:
        gates = StorageGateStore(raw)
        gates.install_schema()
        raw.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_cutover_marker (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                marker INTEGER NOT NULL,
                binding_digest TEXT NOT NULL,
                effect_epoch INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO storage_cutover_marker(id, marker, binding_digest, effect_epoch)
            VALUES (1, 1, 'model-test-binding', 0);
            """
        )
        raw.commit()
        gates.verify_storage_contract(
            GateEvidence(
                run_lease_id="model-test-lease",
                alembic_head="0026_global_wandb_signing_key",
                marker=1,
                binding_digest="model-test-binding",
                contract_version=1,
                evidence_digest="model-test-evidence",
            )
        )
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from titles_api.database import build_engine

    database_url = f"sqlite:///{tmp_path / 'test.sqlite3'}"
    monkeypatch.setenv("TITLES_DATABASE_URL", database_url)
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(tmp_path / "assets"))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("TITLES_EXPORT_ROOT", str(tmp_path / "exports"))
    monkeypatch.setenv("TITLES_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("TITLES_WANDB_INGRESS_BASE_URL", "http://ingress.test")
    engine = build_engine(database_url)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    _install_model_test_foundation(engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSession)
    monkeypatch.setattr(app_module, "engine", engine)
    monkeypatch.setattr(app_module, "upgrade_installed_database", lambda _url: None)
    get_settings.cache_clear()
    app_module.initialize_database()

    def override_db():
        with TestingSession() as db:
            yield db

    launch_contract = LocalLaunchContract(
        allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset({"http://testserver"}),
        synthetic_test_peers=frozenset({"testclient"}),
    )
    application = app_module.create_app(launch_contract)
    application.dependency_overrides[get_db] = override_db
    with TestingSession() as db:
        workspace_id = db.scalar(
            select(models.Workspace.id).order_by(models.Workspace.created_at)
        )
    with TestClient(
        application,
        headers={
            "host": "testserver",
            "origin": "http://testserver",
            "x-workspace-id": str(workspace_id),
        },
    ) as test_client:
        yield test_client
    get_settings.cache_clear()
