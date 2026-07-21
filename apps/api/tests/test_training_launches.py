from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from titles_api import app as app_module
from titles_api import models


def _public_key(seed: int) -> str:
    raw = bytes((seed + offset) % 256 for offset in range(32))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_reusable_credential_creates_one_stable_idempotent_training_launch(client: TestClient) -> None:
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "Portrait LoRA"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        headers={"X-Profile-ID": profile["id"]},
        json={"project_id": project["id"], "name": "Published portraits", "items": []},
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
    with app_module.SessionLocal.begin() as db:
        db.get(models.ImportSource, source["id"]).identity_fingerprint = "source-fingerprint-v1"

    credential_response = client.post(
        "/api/integrations/wandb/credentials",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "project_id": project["id"],
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
        "wandb_credential_id": credential["id"],
        "source_id": source["id"],
        "client_request_id": "launch-request-1",
        "name": "portrait-lora-v4",
        "base_model": "FLUX.1-dev",
        "output_directory": "/workspace/output",
        "checkpoint_policy": {"every_steps": 1000},
        "backup_policy": {"interval_seconds": 60},
        "supported_endpoint_ids": ["ideogram/v4/lora"],
    }
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
    assert manifest["telemetry"]["credential_alias"] == "portrait-wandb"
    assert manifest["backup"]["schema_version"] == "modelfiche.run-backup.v1"
    assert manifest["backup"]["destination"]["prefix"] == f"projects/{project['id']}/runs/{created['run_id']}/"
    assert manifest["backup"]["schedule"] == {
        "interval_seconds": 60,
        "resume_from_remote": True,
        "run_on_finish": True,
    }
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
    for forbidden in ("private_key", "password_manager", "signed_token", "WANDB_API_KEY="):
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
        assert runs[0].status == "pending"
        assert runs[0].wandb_run_id == manifest["telemetry"]["run_id"]
        assert exports[0].payload["dataset_version_ids"] == [dataset["current_version_id"]]

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
    assert {check["code"] for check in readiness["checks"]} >= {
        "EXPORT_READY",
        "SIGNING_CREDENTIAL",
        "STORAGE_SOURCE",
        "PUBLIC_INGRESS",
    }
    assert client.get(f"/api/training-launches/{created['id']}/packet").status_code == 409

    cancelled = client.post(f"/api/training-launches/{created['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "canceled"
    assert client.delete(f"/api/training-launches/{created['id']}").status_code == 204
    assert client.get(f"/api/training-launches/{created['id']}").status_code == 404
