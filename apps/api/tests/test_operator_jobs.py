import io
import zipfile
from datetime import datetime, timezone

from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from PIL import Image
import pytest

from titles_api import app as app_module
from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.integrations.config import CacheSettings
from titles_api.integrations.s3.browser import S3Browser
from titles_api.settings import get_settings
from titles_api.integrations.s3.importer import ImportContext
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink, _supported_fal_endpoint
from titles_worker.export_jobs import ExportPackageHandler
from titles_worker.hydration_jobs import CheckpointHydrationHandler
from titles_worker.import_jobs import S3ImportHandler
from titles_worker.runner import JobRunner


def _project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Operator"}).json()
    return profile, project


def test_checkpoint_hydration_job_downloads_atomically(client: TestClient, monkeypatch):
    _profile, project = _project(client)
    body = b"checkpoint-content"
    source = client.post("/api/import-sources", json={
        "name": "Test S3", "bucket": "models", "allowed_prefixes": ["runs/"],
        "credential_env_prefix": "TESTS3",
    }).json()
    assert source["id"]
    asset = client.post("/api/assets", json={
        "project_id": project["id"], "kind": "model", "name": "step-100.safetensors",
        "location": {"provider": "s3", "uri": "s3://models/runs/one/step-100.safetensors", "bucket": "models", "object_key": "runs/one/step-100.safetensors", "size": len(body)},
    }).json()
    with app_module.SessionLocal.begin() as session:
        remote_location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset["id"],
                models.AssetLocation.provider == "s3",
            )
        )
        remote_location.version_id = "version-100"
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "Run"}).json()
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", json={"step": 100, "asset_id": asset["id"]}).json()
    hydrate_response = client.post(f"/api/checkpoints/{checkpoint['id']}/hydrate")
    assert hydrate_response.status_code == 202
    job = hydrate_response.json()
    assert job["kind"] == "checkpoint.hydrate"
    with app_module.SessionLocal() as session:
        activity = session.scalar(
            select(models.ActivityEvent).where(
                models.ActivityEvent.action == "checkpoint.hydration_queued",
                models.ActivityEvent.subject_id == checkpoint["id"],
            )
        )
        assert activity is not None
        assert activity.details["job_id"] == job["id"]

    class FakeBody(io.BytesIO):
        pass

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_object(self, **kwargs):
            self.calls.append(kwargs)
            return {"Body": FakeBody(body)}

    fake_client = FakeClient()

    class FakeBrowser:
        client = fake_client

        def require_allowed(self, key):
            return key

    monkeypatch.setattr(S3Browser, "from_settings", classmethod(lambda cls, settings: FakeBrowser()))
    settings = get_settings()
    with app_module.SessionLocal.begin() as session:
        asset_row = session.get(models.Asset, asset["id"])
        session.add(models.AssetLocation(
            asset_id=asset_row.id,
            workspace_id=asset_row.workspace_id,
            provider="local",
            uri=str(settings.asset_root / "evicted" / "checkpoint"),
            size=len(body),
            verified_size=len(body),
            verification_state="available",
            hydration_state="hydrated",
        ))
    cache = AssetCache(CacheSettings(root=settings.cache_root))
    runner = JobRunner(app_module.SessionLocal, {"checkpoint.hydrate": CheckpointHydrationHandler(app_module.SessionLocal, cache)})
    assert runner.run_one()
    assert fake_client.calls == [{"Bucket": "models", "Key": "runs/one/step-100.safetensors", "VersionId": "version-100"}]
    completed = client.get(f"/api/jobs/{job['id']}").json()
    assert completed["state"] == "succeeded"
    assert completed["result"]["size"] == len(body)
    assert not completed["result"].get("already_hydrated")
    assert client.get(f"/api/assets/{asset['id']}/content").content == body
    assert client.get(f"/api/checkpoints/{checkpoint['id']}").json()["state"] == "hydrated"
    with app_module.SessionLocal() as session:
        location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset["id"],
                models.AssetLocation.provider == "local",
            )
        )
        assert location is not None
        assert Path(location.uri).resolve().is_relative_to(settings.asset_root.resolve())



def test_run_samples_include_immutable_asset_revision_id(client: TestClient):
    _profile, project = _project(client)
    asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "sample.png"},
    ).json()
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "Run"}).json()
    created = client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": asset["id"], "step": 250}).json()

    sample = client.get(f"/api/runs/{run['id']}/samples").json()[0]

    assert sample["asset_revision_id"] == asset["id"]
    assert created["step"] == 250
    assert sample["training_step"] == 250
    assert sample["sample_track"] is None

def test_run_refresh_normalizes_s3_uri_prefix_before_queueing_import(client: TestClient):
    profile, project = _project(client)
    source = client.post(
        "/api/import-sources",
        json={"name": "Training S3", "bucket": "training", "allowed_prefixes": ["runs/"]},
    ).json()
    run = client.post(
        "/api/runs",
        headers={"X-Profile-ID": profile["id"]},
        json={"project_id": project["id"], "name": "Run"},
    ).json()
    with app_module.SessionLocal.begin() as session:
        row = session.get(models.TrainingRun, run["id"])
        row.source_prefix = "s3://training/runs/faces/"

    response = client.post(
        f"/api/runs/{run['id']}/refresh",
        headers={"X-Profile-ID": profile["id"]},
    )

    assert response.status_code == 202
    with app_module.SessionLocal() as session:
        job = session.get(models.Job, response.json()["job_id"])
        assert job.payload["prefix"] == "runs/faces/"
        assert job.payload["hydrate_dataset_images"] is True
        assert session.get(models.TrainingRun, run["id"]).source_prefix == "runs/faces/"


def test_s3_run_import_infers_project_from_an_unambiguous_trigger(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    modern_times = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "Modern Times", "trigger_words": ["kzapata"]},
    ).json()
    vcribb = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "vcribb", "trigger_words": ["vcribb"]},
    ).json()

    with app_module.SessionLocal() as session:
        sink = SQLAlchemyImportSink(session)
        project_id = sink._run_project_id(
            ImportContext(
                source_id="unused",
                project_id=vcribb["id"],
                prefix="runpod-expanded/titlesxyz-kzapata-ideogram4-v001/samples/",
            ),
            "titlesxyz-kzapata-ideogram4-v001",
        )

    assert project_id == modern_times["id"]

def test_s3_import_maps_supported_base_models_to_fal_endpoints():
    assert _supported_fal_endpoint("krea/Krea-2-Raw") == "fal-ai/krea-2/turbo/lora"
    assert _supported_fal_endpoint("titlesxyz-kzapata-krea2-nocaptions-100s") == "fal-ai/krea-2/turbo/lora"
    assert _supported_fal_endpoint("ideogram-ai/ideogram-4-fp8") == "ideogram/v4/lora"
    assert _supported_fal_endpoint("qwen-image") is None

@pytest.mark.parametrize("kind", ["dataset", "training_run"])
def test_s3_import_hydrates_visual_assets_for_content_and_thumbnails(client: TestClient, monkeypatch, kind):
    _profile, project = _project(client)
    image_stream = io.BytesIO()
    Image.new("RGB", (12, 8), color=(120, 40, 200)).save(image_stream, "PNG")
    image_body = image_stream.getvalue()
    prefix = "datasets/faces/" if kind == "dataset" else "runs/faces/"
    objects = ({
        f"{prefix}captions.jsonl": b'{"image":"one.png","caption":"portrait"}\n',
        f"{prefix}one.png": image_body,
    } if kind == "dataset" else {
        f"{prefix}run_manifest.json": b'{"checkpoints":[],"samples":[]}',
        f"{prefix}run_state.json": b'{"status":"manual_backup_monitoring"}',
        f"{prefix}checkpoints/step-100.safetensors": b"checkpoint",
        f"{prefix}backup_monitoring/status.json": b'{}',
        f"{prefix}evaluation-samples/step-100.png": image_body,
    })
    source = client.post("/api/import-sources", json={
        "name": "Training S3", "bucket": "training", "allowed_prefixes": [prefix.split("/", 1)[0] + "/"],
        "credential_env_prefix": "TESTS3",
    }).json()

    class FakeClient:
        def list_objects_v2(self, **_kwargs):
            return {
                "Contents": [
                    {"Key": key, "Size": len(value), "ETag": f'"etag-{index}"', "LastModified": datetime.now(timezone.utc)}
                    for index, (key, value) in enumerate(objects.items())
                ],
                "IsTruncated": False,
            }

        def head_object(self, *, Key, **_kwargs):
            return {"ContentLength": len(objects[Key])}

        def get_object(self, *, Key, **_kwargs):
            return {"Body": io.BytesIO(objects[Key])}

    monkeypatch.setattr(
        S3Browser,
        "from_settings",
        classmethod(lambda cls, settings: S3Browser(FakeClient(), settings)),
    )
    preview = client.post(f"/api/import-sources/{source['id']}/detect", json={"prefix": prefix})
    assert preview.status_code == 200
    queued = client.post("/api/import-jobs", json={
        "source_id": source["id"], "project_id": project["id"],
        "prefix": prefix, "kind": kind, "hydrate_dataset_images": True,
        "review_token": preview.json()["review_token"],
    }).json()
    settings = get_settings()
    cache = AssetCache(CacheSettings(root=settings.cache_root))
    runner = JobRunner(app_module.SessionLocal, {"s3.import": S3ImportHandler(app_module.SessionLocal, cache)})
    assert runner.run_one()

    import_job = client.get(f"/api/import-jobs/{queued['id']}").json()
    assert import_job["state"] == "succeeded"
    assert import_job["progress"] == 1
    assert import_job["error"] is None
    assert import_job["result"]["hydrated_images"] == {"count": 1, "bytes": len(image_body)}
    if kind == "dataset":
        dataset = client.get("/api/datasets", params={"project_id": project["id"]}).json()[0]
        item = client.get(f"/api/dataset-versions/{dataset['current_version_id']}/items").json()[0]
        asset_id = item["asset_id"]
        assert client.get(f"/api/assets/{asset_id}").json()["project_id"] == project["id"]
    else:
        run = client.get("/api/runs", params={"project_id": project["id"]}).json()[0]
        detail = client.get(f"/api/runs/{run['id']}").json()
        assert detail["status"] == "completed"
        assert detail["stages"] == []
        models_list = client.get("/api/models", params={"project_id": project["id"]}).json()
        assert len(models_list) == 1
        assert models_list[0]["name"] == run["name"]
        assert models_list[0]["version_count"] == 1
        sample = client.get(f"/api/runs/{run['id']}/samples").json()[0]
        asset_id = sample["asset_id"]
        assert sample["asset_revision_id"] == sample["asset_id"]
    locations = client.get(f"/api/assets/{asset_id}/locations").json()
    assert any(location["provider"] == "local" and location["hydration_state"] == "hydrated" for location in locations)
    durable_locations = [
        location for location in locations
        if location["provider"] == "local" and location["hydration_state"] == "hydrated"
        and Path(location["uri"]).is_relative_to(get_settings().asset_root.resolve())
    ]
    assert len(durable_locations) == 1
    assert client.get(f"/api/assets/{asset_id}/content").content == image_body
    thumbnail = client.get(f"/api/assets/{asset_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/webp"


def test_failed_import_synchronizes_state_progress_error_and_allows_retry(client: TestClient, monkeypatch):
    _profile, project = _project(client)
    source = client.post("/api/import-sources", json={
        "name": "Failing S3", "bucket": "training", "allowed_prefixes": ["runs/"],
    }).json()
    class FakeClient:
        def list_objects_v2(self, **_kwargs):
            return {"Contents": [], "IsTruncated": False}

    monkeypatch.setattr(
        S3Browser,
        "from_settings",
        classmethod(lambda cls, settings: S3Browser(FakeClient(), settings)),
    )
    preview = client.post(f"/api/import-sources/{source['id']}/detect", json={"prefix": "runs/failure/"})
    assert preview.status_code == 200
    queued = client.post("/api/import-jobs", json={
        "source_id": source["id"], "project_id": project["id"], "prefix": "runs/failure/",
        "review_token": preview.json()["review_token"],
    }).json()

    def fail_after_progress(context, _payload):
        context.progress(0.4)
        raise RuntimeError("simulated import failure")

    assert JobRunner(app_module.SessionLocal, {"s3.import": fail_after_progress}).run_one()
    failed = client.get(f"/api/import-jobs/{queued['id']}").json()
    assert failed["state"] == "failed"
    assert failed["progress"] == 0.4
    assert failed["error"] == "simulated import failure"
    assert failed["result"]["error"] == "simulated import failure"
    retried = client.post(f"/api/import-jobs/{queued['id']}/retry")
    assert retried.status_code == 202
    assert retried.json()["state"] == "queued"
    assert client.post(f"/api/import-jobs/{queued['id']}/cancel").status_code == 200
    assert JobRunner(app_module.SessionLocal, {"s3.import": fail_after_progress}).run_one()
    canceled = client.get(f"/api/import-jobs/{queued['id']}").json()
    assert canceled["state"] == "canceled"


def test_durable_export_history_manifest_and_download(client: TestClient):
    profile, project = _project(client)
    settings = get_settings()
    local_file = settings.asset_root / "portrait.png"
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_bytes(b"image-data")
    asset = client.post("/api/assets", json={
        "project_id": project["id"], "kind": "image", "name": "portrait.png",
        "location": {"provider": "local", "uri": str(local_file), "size": 10, "hydration_state": "hydrated"},
    }).json()
    client.post("/api/reviews", headers={"X-Profile-ID": profile["id"]}, json={"subject_type": "asset", "subject_id": asset["id"], "rating": 5, "decision": "approved"})
    preview = client.post("/api/transfers/preview", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "Project handoff"})
    assert preview.status_code == 200
    transfer = client.post("/api/transfers", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "Project handoff", "review_token": preview.json()["review_token"]}).json()
    assert transfer["state"] == "queued"
    handler = ExportPackageHandler(app_module.SessionLocal, settings.export_root, (settings.asset_root, settings.cache_root))
    assert JobRunner(app_module.SessionLocal, {"export.package": handler}).run_one()

    status = client.get(f"/api/transfers/{transfer['id']}").json()
    assert status["state"] == "succeeded"
    assert status["download_ready"] is True
    assert client.get("/api/transfers").json()[0]["id"] == transfer["id"]
    response = client.get(f"/api/transfers/{transfer['id']}/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert f"files/{asset['id']}/portrait.png" in archive.namelist()
        manifest = archive.read("manifest.json").decode()
        assert '"decision": "approved"' in manifest

    canceled_preview = client.post("/api/transfers/preview", json={"asset_ids": [asset["id"]], "name": "Canceled export"})
    assert canceled_preview.status_code == 200
    canceled = client.post("/api/transfers", json={"asset_ids": [asset["id"]], "name": "Canceled export", "review_token": canceled_preview.json()["review_token"]}).json()
    assert client.post(f"/api/transfers/{canceled['id']}/cancel").status_code == 200
    assert JobRunner(app_module.SessionLocal, {"export.package": handler}).run_one()
    assert client.get(f"/api/transfers/{canceled['id']}").json()["state"] == "canceled"


def test_operator_settings_are_nonsecret_and_storage_policy_is_not_legacy_config(client: TestClient, monkeypatch):
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAL_KEY", "not-a-real-key")
    monkeypatch.setattr("titles_api.routers.operator_config.validate_fal_connection", lambda _key: None)

    updated = client.patch(
        "/api/operator-settings",
        json={"llm": {"provider": "openai", "model": "gpt-example"}},
    )
    assert updated.status_code == 200
    assert "api_key" not in str(updated.json()).lower()
    assert "storage" not in updated.json()

    legacy_policy = client.patch(
        "/api/operator-settings",
        json={"storage": {"default_source_id": "legacy", "signed_url_ttl": 900}},
    )
    assert legacy_policy.status_code == 422

    fal = client.post("/api/operator-settings/test/fal").json()
    storage = client.post("/api/operator-settings/test/storage").json()
    assert fal["ok"] is True and fal["network_called"] is True
    assert storage["ok"] is True and storage["network_called"] is False
