from __future__ import annotations

from io import BytesIO
import json
import zipfile
from datetime import datetime, timedelta, timezone

from titles_api import app as app_module
from titles_api.models import WorkerHeartbeat


def test_health_reports_worker_unavailable_without_fresh_heartbeat(client) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["components"]["worker_heartbeat"]["status"] == "unavailable"
    assert payload["components"]["queue"]["depth"] == 0


def test_health_reports_fresh_worker_identity_and_version(client) -> None:
    now = datetime.now(timezone.utc)
    with app_module.SessionLocal.begin() as session:
        session.add(WorkerHeartbeat(worker_id="worker-1", state="running", pid=42, version="0.1.0", started_at=now - timedelta(seconds=2), heartbeat_at=now))

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    heartbeat = payload["components"]["worker_heartbeat"]
    assert payload["status"] == "ok"
    assert heartbeat["status"] == "ok"
    assert heartbeat["worker_id"] == "worker-1"
    assert heartbeat["worker_version"] == "0.1.0"
    assert heartbeat["pid"] == 42
    assert heartbeat["age_seconds"] < 10


def test_cli_install_creates_user_scoped_packaged_command(client, tmp_path, monkeypatch) -> None:
    packaged_cli = tmp_path / "Modelfiche.app" / "Contents" / "MacOS" / "mfiche"
    packaged_cli.parent.mkdir(parents=True)
    packaged_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    packaged_cli.chmod(0o755)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TITLES_CLI_PATH", str(packaged_cli))

    response = client.post("/api/system/cli-install")

    assert response.status_code == 200, response.text
    installed = home / ".local" / "bin" / "mfiche"
    assert installed.is_symlink()
    assert installed.resolve() == packaged_cli.resolve()
    doctor = client.get("/api/system/doctor").json()
    assert doctor["agent_cli"]["configured"] is True
    assert doctor["version"] == "0.1.0"
    assert doctor["agent_cli"]["path"] == str(installed)


def test_support_bundle_contains_diagnostics_without_credentials_or_home_paths(client, tmp_path, monkeypatch) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    secret = "super-secret-value"
    log_root.joinpath("api.log").write_text(f"request failed api_key={secret} path={tmp_path}/private\\n", encoding="utf-8")
    monkeypatch.setenv("TITLES_LOG_ROOT", str(log_root))
    monkeypatch.setenv("HOME", str(tmp_path))

    response = client.get("/api/system/support-bundle")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as bundle:
        assert set(bundle.namelist()) == {"report.json", "logs/api.log"}
        report = json.loads(bundle.read("report.json"))
        log = bundle.read("logs/api.log").decode()
    assert report["redaction"]["credentials_included"] is False
    assert secret not in log
    assert str(tmp_path) not in log
    assert "[REDACTED]" in log
