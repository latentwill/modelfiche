import io
import zipfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from titles_api import app as app_module
from titles_api.asset_cache import AssetCache
from titles_api.integrations.config import CacheSettings
from titles_api.integrations.s3.browser import S3Browser
from titles_api.settings import get_settings
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
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "Run"}).json()
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", json={"step": 100, "asset_id": asset["id"]}).json()
    job = client.post(f"/api/checkpoints/{checkpoint['id']}/hydrate").json()
    assert job["kind"] == "checkpoint.hydrate"

    class FakeBody(io.BytesIO):
        pass

    class FakeClient:
        def get_object(self, **_kwargs):
            return {"Body": FakeBody(body)}

    class FakeBrowser:
        client = FakeClient()

        def require_allowed(self, key):
            return key

    monkeypatch.setattr(S3Browser, "from_settings", classmethod(lambda cls, settings: FakeBrowser()))
    settings = get_settings()
    cache = AssetCache(CacheSettings(root=settings.cache_root))
    runner = JobRunner(app_module.SessionLocal, {"checkpoint.hydrate": CheckpointHydrationHandler(app_module.SessionLocal, cache)})
    assert runner.run_one()
    completed = client.get(f"/api/jobs/{job['id']}").json()
    assert completed["state"] == "succeeded"
    assert completed["result"]["size"] == len(body)
    assert client.get(f"/api/assets/{asset['id']}/content").content == body
    assert client.get(f"/api/checkpoints/{checkpoint['id']}").json()["state"] == "hydrated"



def test_run_samples_include_immutable_asset_revision_id(client: TestClient):
    _profile, project = _project(client)
    asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "sample.png"},
    ).json()
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "Run"}).json()
    client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": asset["id"]})

    sample = client.get(f"/api/runs/{run['id']}/samples").json()[0]

    assert sample["asset_revision_id"] == asset["id"]

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
        f"{prefix}samples/step-100.png": image_body,
    })
    source = client.post("/api/import-sources", json={
        "name": "Training S3", "bucket": "training", "allowed_prefixes": [prefix.split("/", 1)[0] + "/"],
        "credential_env_prefix": "TESTS3",
    }).json()
    queued = client.post("/api/import-jobs", json={
        "source_id": source["id"], "project_id": project["id"],
        "prefix": prefix, "kind": kind, "hydrate_dataset_images": True,
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
    assert client.get(f"/api/assets/{asset_id}/content").content == image_body
    thumbnail = client.get(f"/api/assets/{asset_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/webp"


def test_failed_import_synchronizes_state_progress_error_and_allows_retry(client: TestClient):
    _profile, project = _project(client)
    source = client.post("/api/import-sources", json={
        "name": "Failing S3", "bucket": "training", "allowed_prefixes": ["runs/"],
    }).json()
    queued = client.post("/api/import-jobs", json={
        "source_id": source["id"], "project_id": project["id"], "prefix": "runs/failure/",
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
    transfer = client.post("/api/transfers", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "Project handoff"}).json()
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

    canceled = client.post("/api/transfers", json={"asset_ids": [asset["id"]], "name": "Canceled export"}).json()
    assert client.post(f"/api/transfers/{canceled['id']}/cancel").status_code == 200
    assert JobRunner(app_module.SessionLocal, {"export.package": handler}).run_one()
    assert client.get(f"/api/transfers/{canceled['id']}").json()["state"] == "canceled"


def test_operator_settings_are_nonsecret_and_storage_policy_is_not_legacy_config(client: TestClient, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "not-a-real-key")

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
    assert fal["ok"] is True and fal["network_called"] is False
    assert storage["ok"] is True and storage["network_called"] is False
