from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from titles_api import app as app_module
from titles_api import models
from titles_api.wandb_ingress import IngressRateLimiter, create_wandb_ingress_app


GOLDEN = json.loads(Path("tests/compat/wandb/0.28.0/golden.json").read_text())


def _query(operation: str) -> str:
    return next(
        entry["body"]["query"]
        for entry in GOLDEN
        if entry.get("graphql_operation") == operation
    )


def _token(private_key: Ed25519PrivateKey, claims: dict[str, object]) -> str:
    payload = json.dumps(
        claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    signature = private_key.sign(payload)

    def encode(value: bytes) -> str:
        return base64.b32encode(value).decode("ascii").rstrip("=")

    return f"MF1_{encode(payload)}_{encode(signature)}"


def _authorization(token: str) -> str:
    encoded = base64.b64encode(f"api:{token}".encode("ascii")).decode("ascii")
    return f"Basic {encoded}"


def test_ingress_rate_limit_returns_retry_window() -> None:
    current = [100.0]
    limiter = IngressRateLimiter(2, clock=lambda: current[0])
    assert limiter.retry_after("credential") is None
    assert limiter.retry_after("credential") is None
    assert limiter.retry_after("credential") == 60
    current[0] = 161.0
    assert limiter.retry_after("credential") is None


def test_ingress_requires_modelfiche_launch_token_not_vendor_api_key(
    client: TestClient,
    tmp_path: Path,
) -> None:
    ingress_app = create_wandb_ingress_app(
        app_module.SessionLocal,
        upload_root=tmp_path / "wandb-uploads",
        initialize_schema=False,
    )
    vendor_key = base64.b64encode(f"api:{'a' * 40}".encode("ascii")).decode("ascii")

    with TestClient(ingress_app) as ingress:
        missing = ingress.post("/graphql", json={})
        vendor = ingress.post(
            "/graphql",
            headers={"Authorization": f"Basic {vendor_key}"},
            json={},
        )

    assert missing.status_code == 401
    assert (
        missing.json()["detail"]
        == "Modelfiche launch token is required; a wandb.ai API key is not used"
    )
    assert (
        missing.headers["www-authenticate"] == 'Basic realm="ModelFiche telemetry shim"'
    )
    assert vendor.status_code == 401
    assert (
        vendor.json()["detail"]
        == "expected an MF1 Modelfiche launch token, not a wandb.ai API key"
    )


def test_expired_signed_token_remains_valid_only_while_run_is_active(
    client: TestClient,
    tmp_path: Path,
) -> None:
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "Long Training Project"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        headers={"X-Profile-ID": profile["id"]},
        json={"project_id": project["id"], "name": "Training data", "items": []},
    ).json()
    source = client.post(
        "/api/import-sources",
        json={
            "name": "Training S3",
            "bucket": "training",
            "allowed_prefixes": [f"projects/{project['id']}/"],
        },
    ).json()
    with app_module.SessionLocal.begin() as db:
        db.get(
            models.ImportSource, source["id"]
        ).identity_fingerprint = "verified-source"
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    credential = client.post(
        "/api/integrations/wandb/credentials",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "alias": "long-run-key",
            "public_key": base64.urlsafe_b64encode(public_bytes)
            .decode("ascii")
            .rstrip("="),
        },
    ).json()
    launch = client.post(
        "/api/training-launches",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "dataset_version_id": dataset["current_version_id"],
            "source_id": source["id"],
            "client_request_id": "long-ingress-launch",
            "name": "long-ingress-run",
            "base_model": "FLUX.1-dev",
            "output_directory": "/workspace/output",
        },
    ).json()
    telemetry = launch["manifest"]["telemetry"]
    claims = dict(telemetry["token"]["claims"])
    claims["expires_at"] = "2000-01-01T00:00:00Z"
    with app_module.SessionLocal.begin() as db:
        launch_record = db.get(models.TrainingLaunch, launch["id"])
        run = db.get(models.TrainingRun, launch["run_id"])
        manifest = json.loads(json.dumps(launch_record.redacted_manifest))
        manifest["telemetry"]["token"]["claims"] = claims
        launch_record.redacted_manifest = manifest
        launch_record.state = "running"
        run.status = "completed"
        run.started_at = models.utcnow()
    auth = {"Authorization": _authorization(_token(private_key, claims))}
    ingress_app = create_wandb_ingress_app(
        app_module.SessionLocal,
        upload_root=tmp_path / "wandb-uploads",
        initialize_schema=False,
    )
    request = {
        "operationName": "ServerFeaturesQuery",
        "query": _query("ServerFeaturesQuery"),
        "variables": {},
    }
    with TestClient(ingress_app) as ingress:
        assert ingress.post("/graphql", headers=auth, json=request).status_code == 200
        stream_path = (
            f"/files/dam/{telemetry['project']}/{telemetry['run_id']}/file_stream"
        )
        stream = ingress.post(
            stream_path,
            headers=auth,
            json={
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 0,
                        "content": [json.dumps({"_step": 1, "loss": 1.0})],
                    }
                }
            },
        )
        assert stream.status_code == 200
        with app_module.SessionLocal() as db:
            assert db.get(models.TrainingRun, launch["run_id"]).status == "running"
        with app_module.SessionLocal.begin() as db:
            db.get(models.TrainingLaunch, launch["id"]).state = "completed"
            db.get(models.TrainingRun, launch["run_id"]).status = "completed"
        expired = ingress.post("/graphql", headers=auth, json=request)
        assert expired.status_code == 401
        assert expired.json()["detail"] == "Modelfiche launch token has expired"


def test_pinned_graphql_filestream_resume_upload_and_finish_contract(
    client: TestClient,
    tmp_path: Path,
) -> None:
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "Ingress Project"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        headers={"X-Profile-ID": profile["id"]},
        json={"project_id": project["id"], "name": "Training data", "items": []},
    ).json()
    source = client.post(
        "/api/import-sources",
        json={
            "name": "Ingress S3",
            "bucket": "training",
            "allowed_prefixes": [f"projects/{project['id']}/"],
        },
    ).json()
    with app_module.SessionLocal.begin() as db:
        db.get(
            models.ImportSource, source["id"]
        ).identity_fingerprint = "verified-source"

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
    credential = client.post(
        "/api/integrations/wandb/credentials",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "alias": "ingress-key",
            "public_key": public_key,
        },
    ).json()
    launch = client.post(
        "/api/training-launches",
        headers={"X-Profile-ID": profile["id"]},
        json={
            "dataset_version_id": dataset["current_version_id"],
            "source_id": source["id"],
            "client_request_id": "ingress-launch-1",
            "name": "ingress-run",
            "trainer": "kef-krea2",
            "base_model": "FLUX.1-dev",
            "output_directory": "/workspace/output",
        },
    ).json()
    telemetry = launch["manifest"]["telemetry"]
    auth = {
        "Authorization": _authorization(
            _token(private_key, telemetry["token"]["claims"])
        )
    }

    ingress_app = create_wandb_ingress_app(
        app_module.SessionLocal,
        upload_root=tmp_path / "wandb-uploads",
        initialize_schema=False,
    )
    with TestClient(ingress_app) as ingress:
        features = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "ServerFeaturesQuery",
                "query": _query("ServerFeaturesQuery"),
                "variables": {},
            },
        )
        assert features.status_code == 200
        assert features.json() == {"data": {"serverInfo": {"features": []}}}
        assert ingress.post("/graphql", json={}).status_code == 401
        unsupported = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "Unknown",
                "query": "query Unknown { viewer { id } }",
                "variables": {},
            },
        )
        assert unsupported.status_code == 422
        assert (
            unsupported.json()["errors"][0]["extensions"]["code"]
            == "UNSUPPORTED_OPERATION"
        )
        oversized = ingress.post(
            "/graphql",
            headers={**auth, "Content-Length": str(1024 * 1024 + 1)},
            content=b"{}",
        )
        assert oversized.status_code == 413

        upsert_variables = {
            "id": None,
            "name": telemetry["run_id"],
            "project": telemetry["project"],
            "entity": "dam",
            "displayName": "ingress-run",
            "description": None,
            "config": {
                "learning_rate": {"value": 0.0001},
                "nested": {"value": {"enabled": True}},
                "api_token": {"value": "must-not-persist"},
                "optimizer": {"value": {"name": "AdamW", "password": "also-secret"}},
                "apiKey": {"value": "camel-secret"},
                "service-secret": {"value": "punctuated-secret"},
            },
            "commit": None,
            "host": "trainer",
            "debug": None,
            "program": "train.py",
            "repo": None,
            "jobType": None,
            "state": None,
            "sweep": None,
            "groupName": None,
            "tags": None,
            "summaryMetrics": None,
            "notes": None,
        }
        upsert = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "UpsertBucket",
                "query": _query("UpsertBucket"),
                "variables": upsert_variables,
            },
        )
        assert upsert.status_code == 200
        assert upsert.json()["data"]["upsertBucket"]["inserted"] is True
        resumed = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "UpsertBucket",
                "query": _query("UpsertBucket"),
                "variables": upsert_variables,
            },
        )
        assert resumed.status_code == 200
        assert resumed.json()["data"]["upsertBucket"]["inserted"] is False
        handoff_url = f"/checkpoint-handoffs/{launch['run_id']}"
        handoff_body = {
            "schema_version": "modelfiche.checkpoint-manifest.v1",
            "generation": 1,
            "manifest_key": (
                f"{launch['manifest']['artifact_handoff']['manifest_prefix']}1.json"
            ),
            "manifest_sha256": "a" * 64,
            "manifest_etag": "manifest-etag",
            "manifest_version_id": "manifest-version",
            "checkpoint_count": 2,
        }
        observed = ingress.post(handoff_url, headers=auth, json=handoff_body)
        assert observed.status_code == 201
        assert observed.json()["state"] == "observed"
        assert (
            ingress.post(handoff_url, headers=auth, json=handoff_body).status_code
            == 200
        )
        conflict = ingress.post(
            handoff_url,
            headers=auth,
            json={**handoff_body, "manifest_sha256": "b" * 64},
        )
        assert conflict.status_code == 409

        stream_path = (
            f"/files/dam/{telemetry['project']}/{telemetry['run_id']}/file_stream"
        )
        batch = {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [
                        {
                            "_step": 2,
                            "_timestamp": 100.0,
                            "loss/denoise": 0.4,
                            "loss": 0.4,
                            "grad_norm": 1.5,
                            "embedding/token_residual_rms": 0.03,
                            "learning_rate": 0.0001,
                            "phase": "training",
                        }
                    ],
                },
                "wandb-summary.json": {
                    "offset": 0,
                    "content": [{"_step": 2, "loss/denoise": 0.4, "phase": "training"}],
                },
            }
        }
        first = ingress.post(stream_path, headers=auth, json=batch)
        duplicate = ingress.post(stream_path, headers=auth, json=batch)
        assert first.status_code == duplicate.status_code == 200
        assert first.json() == duplicate.json() == {}
        out_of_order = ingress.post(
            stream_path,
            headers=auth,
            json={
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 1,
                        "content": [
                            {"_step": 1, "_timestamp": 99.0, "loss/denoise": 0.5}
                        ],
                    }
                }
            },
        )
        assert out_of_order.status_code == 200
        auxiliary_stream = ingress.post(
            stream_path,
            headers=auth,
            json={
                "files": {
                    "output.log": {"offset": 0, "content": ["training started"]},
                    "wandb-events.jsonl": {
                        "offset": 0,
                        "content": ['{"event": "heartbeat"}'],
                    },
                }
            },
        )
        assert auxiliary_stream.status_code == 200

        instructions = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "CreateRunFiles",
                "query": _query("CreateRunFiles"),
                "variables": {
                    "entity": "dam",
                    "project": telemetry["project"],
                    "run": telemetry["run_id"],
                    "files": ["wandb-summary.json", "media/images/sample.png"],
                },
            },
        )
        assert instructions.status_code == 200
        unsupported_file = ingress.post(
            "/graphql",
            headers=auth,
            json={
                "operationName": "CreateRunFiles",
                "query": _query("CreateRunFiles"),
                "variables": {
                    "entity": "dam",
                    "project": telemetry["project"],
                    "run": telemetry["run_id"],
                    "files": ["artifact.bin"],
                },
            },
        )
        assert unsupported_file.status_code == 422
        assert "unsupported W&B file upload" in unsupported_file.text
        upload_urls = {
            item["name"]: item["uploadUrl"]
            for item in instructions.json()["data"]["createRunFiles"]["files"]
        }
        media = b"\x89PNG\r\n\x1a\nsample"
        media_path = upload_urls["media/images/sample.png"].removeprefix(
            "http://testserver"
        )
        assert ingress.put(media_path, content=media).status_code == 200
        summary_body = json.dumps(
            {
                "_step": 2,
                "loss/denoise": 0.4,
                "sample_7": {
                    "_type": "image-file",
                    "path": "media/images/sample.png",
                    "caption": "test sample",
                    "sha256": hashlib.sha256(media).hexdigest(),
                    "size": len(media),
                    "format": "png",
                    "width": 2,
                    "height": 2,
                },
            },
            separators=(",", ":"),
        ).encode()
        upload_path = upload_urls["wandb-summary.json"].removeprefix(
            "http://testserver"
        )
        uploaded = ingress.put(upload_path, content=summary_body)
        assert uploaded.status_code == 200
        assert ingress.put(upload_path, content=summary_body).status_code == 200
        assert (
            ingress.post(
                stream_path, headers=auth, json={"complete": True, "exitcode": 0}
            ).status_code
            == 200
        )
        assert (
            ingress.post(
                stream_path, headers=auth, json={"complete": True, "exitcode": 0}
            ).status_code
            == 200
        )

    live = client.get(f"/api/runs/{launch['run_id']}/live")
    assert live.status_code == 200
    assert live.json()["status"] == "completed"
    assert live.json()["current_step"] == 2
    assert live.json()["latest_loss"]["value"] == 0.4
    assert live.json()["learning_rate"]["value"] == 0.0001
    assert live.json()["uploads"] == {"uploaded": 2}
    events = client.get(f"/api/runs/{launch['run_id']}/events")
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: run.snapshot" in events.text
    assert '"status":"completed"' in events.text

    with app_module.SessionLocal() as db:
        run = db.get(models.TrainingRun, launch["run_id"])
        assert run.status == "completed"
        assert run.current_step == 2
        assert run.normalized_config["wandb"]["learning_rate"] == 0.0001
        assert run.raw_manifest["wandb_config"]["nested"]["value"] == {"enabled": True}
        assert run.normalized_config["wandb"]["api_token"] == "<redacted>"
        assert run.normalized_config["wandb"]["optimizer"]["password"] == "<redacted>"
        assert run.normalized_config["wandb"]["apiKey"] == "<redacted>"
        assert run.normalized_config["wandb"]["service-secret"] == "<redacted>"
        assert "must-not-persist" not in json.dumps(run.raw_manifest)
        assert "also-secret" not in json.dumps(run.raw_manifest)
        assert "camel-secret" not in json.dumps(run.raw_manifest)
        assert "punctuated-secret" not in json.dumps(run.raw_manifest)
        metrics = (
            db.query(models.TrainingMetric)
            .order_by(models.TrainingMetric.step, models.TrainingMetric.name)
            .all()
        )
        assert [(row.step, row.name, row.value, row.value_text) for row in metrics] == [
            (1, "loss/denoise", 0.5, None),
            (2, "embedding/token_residual_rms", 0.03, None),
            (2, "grad_norm", 1.5, None),
            (2, "learning_rate", 0.0001, None),
            (2, "loss", 0.4, None),
            (2, "loss/denoise", 0.4, None),
            (2, "phase", None, "training"),
        ]
        summaries = {row.name: row for row in db.query(models.RunMetricSummary).all()}
        assert summaries["loss/denoise"].last_step == 2
        assert summaries["embedding/token_residual_rms"].last_value == 0.03
        assert summaries["phase"].last_value_text == "training"
        assert db.query(models.RunEvent).filter_by(type="run.completed").count() == 1
        uploads = {row.relative_path: row for row in db.query(models.RunUpload).all()}
        summary_upload = uploads["wandb-summary.json"]
        assert summary_upload.state == "uploaded"
        assert (
            Path(summary_upload.metadata_["staged_path"]).read_bytes() == summary_body
        )
        media_upload = uploads["media/images/sample.png"]
        assert media_upload.state == "uploaded"
        assert media_upload.step == 2
        assert media_upload.caption == "test sample"
        assert media_upload.expected_sha256 == hashlib.sha256(media).hexdigest()
        assert Path(media_upload.metadata_["staged_path"]).read_bytes() == media
        handoff = db.query(models.RunArtifactHandoff).one()
        assert handoff.state == "observed"
        assert handoff.checkpoint_count == 2
