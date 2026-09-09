from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from titles_api import app as app_module
from titles_api import models
from titles_api.training_launches import _trainer_configuration


def _public_key(seed: int) -> str:
    raw = bytes((seed + offset) % 256 for offset in range(32))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def test_wandb_signing_key_is_one_global_reconcilable_credential(
    client: TestClient,
) -> None:
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    first = client.post(
        "/api/integrations/wandb/credentials",
        headers=headers,
        json={"alias": "first-project", "public_key": _public_key(1)},
    )
    second = client.post(
        "/api/integrations/wandb/credentials",
        headers=headers,
        json={"alias": "modelfiche", "public_key": _public_key(2)},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["scope"] == "global"
    assert second.json()["public_key"] == _public_key(2)
    other_workspace = client.post("/api/workspaces", json={"name": "Other"}).json()
    listed = client.get(
        "/api/integrations/wandb/credentials",
        headers={"X-Workspace-ID": other_workspace["slug"]},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [first.json()["id"]]





def test_global_credential_creates_one_stable_idempotent_training_launch(
    client: TestClient,
) -> None:
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "Airbrush"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        headers={"X-Profile-ID": profile["id"]},
        json={"project_id": project["id"], "name": "Airbrush training", "items": []},
    ).json()
    source = client.post(
        "/api/import-sources",
        json={
            "name": "Training S3",
            "bucket": "training",
            "allowed_prefixes": [f"projects/{project['id']}/"],
            "credential_env_prefix": "TRAINING_S3",
        },
    ).json()

    credential_response = client.post(
        "/api/integrations/wandb/credentials",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "alias": "portrait-wandb",
            "public_key": _public_key(1),
        },
    )
    assert credential_response.status_code == 201
    credential = credential_response.json()
    assert credential["state"] == "active"
    assert credential["public_key"] == _public_key(1)

    body = {
        "dataset_version_id": dataset["current_version_id"],
        "source_id": source["id"],
        "client_request_id": "launch-request-1",
        "name": "portrait-kef-v4",
        "trainer": "kef-krea2",
        "base_model": "krea/Krea-2-Raw",
        "output_directory": "/workspace/output",
        "live_telemetry": False,
        "training_config": {
            "steps": 1234,
            "num_tokens": 7,
            "learning_rate": 0.0002,
            "gradient_accumulation": 4,
            "resolution": 768,
            "sample_interval": 250,
            "sample_steps": 16,
            "sample_resolution": 512,
            "caption_mode": "none",
        },
        "checkpoint_policy": {"every_steps": 1000},
        "backup_policy": {"interval_seconds": 60},
        "supported_endpoint_ids": ["ideogram/v4/lora"],
    }
    # An explicit offline launch remains available even with live ingress configured.
    created_response = client.post(
        "/api/training-launches",
        headers={"X-Profile-ID": profile["id"]},
        json=body,
    )
    assert created_response.status_code == 201
    created = created_response.json()
    manifest = created["manifest"]
    assert created["state"] == "preparing"
    assert manifest["schema_version"] == "modelfiche.training-launch.v1"
    assert manifest["run"]["dataset_version_id"] == dataset["current_version_id"]
    assert manifest["telemetry"]["sdk_version"] == "0.28.0"
    assert (
        manifest["telemetry"]["protocol_revision"]
        == "wandb-0.28.0-modelfiche-trainers-v1"
    )
    trainer = _trainer_configuration(manifest)
    assert trainer["id"] == "kef-krea2"
    assert trainer["telemetry"]["enabled"] is False
    assert (
        trainer["telemetry"]["metric_mapping"]["token_residual_rms"]
        == "embedding/token_residual_rms"
    )
    assert trainer["configuration"]["num_tokens"] == 7
    assert trainer["configuration"]["gradient_accumulation"] == 4
    assert trainer["configuration"]["caption_mode"] == "none"
    argv = trainer["command"]["argv"]
    assert argv[:3] == [
        "kef-krea2-train",
        "training-dataset",
        "/workspace/output",
    ]
    assert argv[argv.index("--steps") + 1] == "1234"
    assert argv[argv.index("--tokens") + 1] == "7"
    assert argv[argv.index("--gradient-accumulation") + 1] == "4"
    assert argv[argv.index("--resolution") + 1] == "768"
    assert argv[argv.index("--sample-interval") + 1] == "250"
    assert argv[argv.index("--sample-steps") + 1] == "16"
    assert argv[argv.index("--sample-resolution") + 1] == "512"
    assert argv[argv.index("--caption-mode") + 1] == "none"
    assert manifest["telemetry"]["credential_alias"] == "portrait-wandb"
    assert manifest["backup"]["schema_version"] == "modelfiche.run-backup.v1"
    assert (
        manifest["backup"]["destination"]["prefix"]
        == f"projects/{project['id']}/runs/{created['run_id']}/"
    )
    assert manifest["backup"]["schedule"] == {
        "interval_seconds": 60,
        "resume_from_remote": True,
        "run_on_finish": True,
    }
    assert manifest["backup"]["sync_sets"] == [
        {"local": "checkpoints/", "remote": "checkpoints/"},
        {"local": "logs/", "remote": "logs/", "refresh": True},
        {"local": "configs/", "remote": "configs/"},
        {"local": "samples/", "remote": "samples/", "refresh": True},
        {"local": "wandb/", "remote": "wandb/", "refresh": True},
    ]
    assert manifest["backup"]["checkpoint_handoff"]["write_order"] == [
        "checkpoint_objects",
        "manifest_object",
        "callback",
    ]
    assert manifest["backup"]["checkpoint_handoff"]["callback_required"] is False
    assert manifest["artifact_handoff"]["manifest_callback_url"].endswith(
        f"/checkpoint-handoffs/{created['run_id']}"
    )
    assert manifest["dataset"]["download_url"].endswith(
        f"/api/transfers/{created['dataset_export_job_id']}/download"
    )
    serialized = json.dumps(created, sort_keys=True)
    for forbidden in (
        "private_key",
        "password_manager",
        "signed_token",
        "WANDB_API_KEY=",
    ):
        assert forbidden not in serialized

    repeated_response = client.post(
        "/api/training-launches",
        headers={"X-Profile-ID": profile["id"]},
        json=body,
    )
    assert repeated_response.status_code == 200
    assert repeated_response.json()["id"] == created["id"]
    assert repeated_response.json()["manifest_digest"] == created["manifest_digest"]

    changed = {**body, "name": "different-name"}
    conflict = client.post(
        "/api/training-launches",
        headers={"X-Profile-ID": profile["id"]},
        json=changed,
    )
    assert conflict.status_code == 409
    assert "different launch request" in conflict.json()["detail"]

    first_manifest = client.get(f"/api/training-launches/{created['id']}/manifest")
    second_manifest = client.get(f"/api/training-launches/{created['id']}/manifest")
    assert first_manifest.content == second_manifest.content
    assert first_manifest.json() == manifest
    assert "attachment" in first_manifest.headers["content-disposition"]

    environment = client.get(f"/api/training-launches/{created['id']}/environment").text
    assert 'export WANDB_RESUME="allow"' in environment
    assert "MF1_BASE32_ED25519_SIGNED_TOKEN" in environment
    assert credential["public_key"] not in environment

    with app_module.SessionLocal() as db:
        runs = db.query(models.TrainingRun).all()
        launches = db.query(models.TrainingLaunch).all()
        exports = db.query(models.Job).filter_by(kind="export.package").all()
        assert len(runs) == len(launches) == len(exports) == 1
        assert launches[0].source_fingerprint is not None
        assert runs[0].status == "pending"
        assert runs[0].wandb_run_id == manifest["telemetry"]["run_id"]
        assert exports[0].payload["dataset_version_ids"] == [
            dataset["current_version_id"]
        ]

    rotated = client.post(
        f"/api/integrations/wandb/credentials/{credential['id']}/rotate",
        headers={"X-Profile-ID": profile["id"]},
        json={"public_key": _public_key(2)},
    )
    assert rotated.status_code == 200
    assert rotated.json()["key_id"] != credential["key_id"]
    revoked = client.post(
        f"/api/integrations/wandb/credentials/{credential['id']}/revoke",
        headers={"X-Profile-ID": profile["id"]},
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    preflight = client.get(f"/api/training-launches/{created['id']}/preflight")
    assert preflight.status_code == 200
    readiness = preflight.json()
    assert readiness["ready_to_start"] is False
    assert readiness["safe_to_retry"] is True
    assert {
        "EXPORT_READY",
        "SIGNING_CREDENTIAL",
        "STORAGE_SOURCE",
        "TRAINER_TRANSPORT",
    } <= {check["code"] for check in readiness["checks"]}
    assert (
        client.get(f"/api/training-launches/{created['id']}/packet").status_code == 409
    )

    cancelled = client.post(f"/api/training-launches/{created['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "canceled"
    assert client.delete(f"/api/training-launches/{created['id']}").status_code == 204
    assert client.get(f"/api/training-launches/{created['id']}").status_code == 404


def test_connected_launch_requires_ingress_before_creating_work(client, monkeypatch):
    from sqlalchemy import func, select
    from titles_api.settings import get_settings

    monkeypatch.delenv("TITLES_WANDB_INGRESS_BASE_URL", raising=False)
    get_settings.cache_clear()
    project = client.post("/api/projects", json={"title": "Telemetry required"}).json()
    dataset = client.post("/api/datasets", json={
        "project_id": project["id"], "name": "Training data", "items": [],
    }).json()
    source = client.post("/api/import-sources", json={
        "name": "Training", "bucket": "training",
    }).json()
    body = {
        "dataset_version_id": dataset["current_version_id"],
        "source_id": source["id"],
        "client_request_id": "connected-launch",
        "name": "Connected", "base_model": "FLUX.1-dev",
        "output_directory": "/workspace/output",
    }
    for telemetry in (None, True):
        response = client.post("/api/training-launches", json={**body, "live_telemetry": telemetry})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "training_telemetry_not_configured"
    with app_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(models.TrainingLaunch)) == 0
        assert db.scalar(select(func.count()).select_from(models.Job)) == 0
    readiness = client.get("/api/training-setup").json()
    assert readiness["ready"] is False
    assert next(check for check in readiness["checks"] if check["code"] == "LIVE_TELEMETRY")["status"] == "blocked"


def test_live_environment_overrides_stale_sdk_mode_and_entity():
    import os
    import subprocess
    import sys
    import shlex
    from titles_api.training_launches import launch_environment

    launch = models.TrainingLaunch(redacted_manifest={"telemetry": {
        "live_enabled": True,
        "base_url": "http://ingress.test",
        "entity": "dam",
        "project": "expected-project",
        "run_id": "expected-run",
        "credential_alias": "global",
        "token": {"claims": {}},
    }})
    script = launch_environment(launch)
    script += shlex.join([sys.executable, "-c", "import json, os; print(json.dumps({key: os.environ[key] for key in ('WANDB_MODE', 'WANDB_ENTITY', 'WANDB_RUN_ID', 'WANDB_BASE_URL')}))"])
    result = subprocess.run(
        ["sh", "-c", script],
        env={**os.environ, "WANDB_MODE": "offline", "WANDB_ENTITY": "vendor-account"},
        check=True, text=True, capture_output=True,
    )
    assert json.loads(result.stdout) == {
        "WANDB_MODE": "online",
        "WANDB_ENTITY": "dam",
        "WANDB_RUN_ID": "expected-run",
        "WANDB_BASE_URL": "http://ingress.test",
    }
