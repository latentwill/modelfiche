import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from titles_api import app as app_module
from titles_api.database import Base, get_db
from titles_api.settings import get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    from titles_api.database import build_engine

    monkeypatch.setenv("TITLES_ASSET_ROOT", str(tmp_path / "assets"))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("TITLES_EXPORT_ROOT", str(tmp_path / "exports"))
    engine = build_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSession)
    monkeypatch.setattr(app_module, "engine", engine)
    get_settings.cache_clear()
    app_module.initialize_database()

    def override_db():
        with TestingSession() as db:
            yield db

    application = app_module.create_app()
    application.dependency_overrides[get_db] = override_db
    with TestClient(application) as test_client:
        yield test_client
    get_settings.cache_clear()
