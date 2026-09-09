from __future__ import annotations

import stat
from pathlib import Path

from fastapi.testclient import TestClient

from titles_api.integrations.config import FalSettings
from titles_api.integrations.fal.credentials import FalSecretStore
from titles_api.integrations.fal.validation import validate_fal_connection


def test_secret_store_permissions_reload_clear_and_env_precedence(tmp_path: Path, monkeypatch):
    store = FalSecretStore(tmp_path / "config")
    store.save("saved-secret")

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert FalSecretStore(tmp_path / "config").resolve({}).key == "saved-secret"
    assert store.resolve({"FAL_API_KEY": "api-env"}).key == "api-env"
    credential = store.resolve({"FAL_KEY": "primary-env", "FAL_API_KEY": "api-env"})
    assert credential and credential.key == "primary-env" and credential.source == "FAL_KEY"

    store.clear()
    assert store.resolve({}) is None
    assert not store.path.exists()


def test_fal_settings_uses_saved_secret(client: TestClient, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_API_KEY", raising=False)
    response = client.put("/api/operator-settings/fal-key", json={"key": "stored-only"})
    assert response.status_code == 200
    assert "stored-only" not in response.text
    assert FalSettings.from_env().api_key == "stored-only"

    reloaded = client.get("/api/operator-settings")
    assert reloaded.json()["fal_connection"]["source"] == "saved"
    assert "stored-only" not in reloaded.text
    cleared = client.delete("/api/operator-settings/fal-key")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_operator_fal_validation_success_and_failure(client: TestClient, monkeypatch):
    client.put("/api/operator-settings/fal-key", json={"key": "test-key"})
    monkeypatch.setattr("titles_api.routers.operator_config.validate_fal_connection", lambda key: None)
    success = client.post("/api/operator-settings/test/fal")
    assert success.status_code == 200
    assert success.json()["mode"] == "network_validated"
    assert success.json()["network_called"] is True
    assert client.get("/api/operator-settings").json()["fal_connection"]["last_validation"]["state"] == "validated"

    def rejected(_key: str) -> None:
        raise ValueError("FAL rejected the credential")

    monkeypatch.setattr("titles_api.routers.operator_config.validate_fal_connection", rejected)
    failure = client.post("/api/operator-settings/test/fal")
    assert failure.status_code == 401
    assert "rejected" in failure.json()["detail"]
    assert "test-key" not in failure.text


def test_nonbillable_validation_transport_success_and_failure():
    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class Transport:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response(self.status_code)

    ok = Transport(200)
    validate_fal_connection("secret", ok)
    assert ok.calls[0][0] == "GET"
    assert "/v1/models?limit=1" in ok.calls[0][1]
    assert "secret" not in ok.calls[0][1]

    denied = Transport(401)
    try:
        validate_fal_connection("secret", denied)
    except ValueError as exc:
        assert "rejected" in str(exc)
    else:
        raise AssertionError("expected invalid credentials to fail")
