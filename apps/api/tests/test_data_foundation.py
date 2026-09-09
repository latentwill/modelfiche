import base64
from io import BytesIO
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import select

from titles_api import app as app_module, models
from titles_api.routers import integrations, local_imports
from titles_api.settings import get_settings


def _project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "DATA project"}).json()
    return headers, project


def test_local_folder_import_preserves_paths_sidecars_duplicates_and_partial_failures(client: TestClient):
    _headers, project = _project(client)
    image = base64.b64encode(b"same-image").decode()
    response = client.post("/api/local-imports", json={
        "project_id": project["id"], "dataset_name": "Folder dataset", "files": [
            {"path": "portraits/a.png", "content_base64": image},
            {"path": "portraits/a.txt", "content_base64": base64.b64encode(b"first caption").decode()},
            {"path": "portraits/a.json", "content_base64": base64.b64encode(b'{"camera":"virtual"}').decode()},
            {"path": "alternates/a.png", "content_base64": image},
            {"path": "broken.png", "content_base64": "not-base64"},
        ],
    })
    assert response.status_code == 201
    result = response.json()
    assert result["state"] == "completed_with_errors"
    assert (result["imported"], result["duplicates"], len(result["failures"])) == (1, 1, 1)
    detail = client.get(f"/api/datasets/{result['dataset_id']}").json()
    items = client.get(f"/api/dataset-versions/{detail['current_version_id']}/items").json()
    assert len(items) == 1
    assert items[0]["caption"] == "first caption"
    assert all(item["origin_type"] == "DATASET" and item["availability"] == "available" for item in items)
 
 
def test_local_folder_import_extracts_dimensions_and_embedded_generation_metadata(client: TestClient):
    _headers, project = _project(client)
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", '{"prompt":"a red kite","seed":17}')
    image_buffer = BytesIO()
    Image.new("RGB", (13, 7), (220, 30, 30)).save(image_buffer, format="PNG", pnginfo=info)
    response = client.post("/api/local-imports", json={
        "project_id": project["id"],
        "dataset_name": "Metadata dataset",
        "files": [{"path": "kite.png", "content_base64": base64.b64encode(image_buffer.getvalue()).decode()}],
    })
    assert response.status_code == 201, response.text
    with app_module.SessionLocal() as db:
        asset = db.scalar(select(models.Asset).where(models.Asset.project_id == project["id"]))
        assert asset is not None
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
        assert image is not None
        assert (image.width, image.height, image.color_mode) == (13, 7, "RGB")
        assert asset.metadata_["generation_metadata"]["parameters"]["seed"] == 17
 
 
def test_local_folder_import_extracts_nested_jpeg_user_comment(client: TestClient):
    _headers, project = _project(client)
    exif = Image.Exif()
    exif[34665] = {
        36867: "2026:01:01 00:00:00",
        37510: b'ASCII\x00\x00\x00{"seed":23,"prompt":"night flight"}',
    }
    image_buffer = BytesIO()
    Image.new("RGB", (9, 5), (20, 40, 80)).save(image_buffer, format="JPEG", exif=exif)
    response = client.post("/api/local-imports", json={
        "project_id": project["id"],
        "dataset_name": "JPEG metadata dataset",
        "files": [{"path": "night.jpg", "content_base64": base64.b64encode(image_buffer.getvalue()).decode()}],
    })
    assert response.status_code == 201, response.text
    with app_module.SessionLocal() as db:
        asset = db.scalar(select(models.Asset).where(models.Asset.project_id == project["id"]))
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
        assert image is not None
        assert (image.width, image.height) == (9, 5)
        assert asset.metadata_["generation_metadata"]["exif_UserComment"]["seed"] == 23


def test_local_folder_import_reports_unsupported_and_duplicate_paths(client: TestClient):
    _headers, project = _project(client)
    image = base64.b64encode(b"nested-image").decode()
    response = client.post("/api/local-imports", json={
        "project_id": project["id"], "dataset_name": "Folder diagnostics", "files": [
            {"path": "nested/image.png", "content_base64": image},
            {"path": "nested/image.png", "content_base64": image},
            {"path": "nested/readme.exe", "content_base64": base64.b64encode(b"unsupported").decode()},
        ],
    })
    assert response.status_code == 201
    result = response.json()
    assert result["imported"] == 1
    assert result["duplicates"] == 0
    assert {failure["path"] for failure in result["failures"]} == {"nested/image.png", "nested/readme.exe"}
    assert any("duplicate path" in failure["error"] for failure in result["failures"])
    assert any("unsupported file type" in failure["error"] for failure in result["failures"])

def test_local_folder_import_removes_written_files_when_database_work_fails(client: TestClient, monkeypatch):
    _headers, project = _project(client)
    monkeypatch.setattr(local_imports, "record_activity", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced persistence failure")))
    with pytest.raises(RuntimeError, match="forced persistence failure"):
        client.post("/api/local-imports", json={
            "project_id": project["id"], "dataset_name": "Rollback dataset", "files": [
                {"path": "folder/image.png", "content_base64": base64.b64encode(b"image").decode()},
            ],
        })
    import_root = get_settings().asset_root.resolve() / "local-imports"
    assert not import_root.exists() or not any(import_root.rglob("*"))
    assert all(row["name"] != "Rollback dataset" for row in client.get("/api/datasets").json())


def test_dataset_detail_exposes_explicit_training_run_and_empty_reason(client: TestClient):
    headers, project = _project(client)
    dataset = client.post("/api/datasets", headers=headers, json={"project_id": project["id"], "name": "Missing source", "items": []}).json()
    detail = client.get(f"/api/datasets/{dataset['id']}").json()
    assert detail["empty_reason"] == "No source image objects were imported for this dataset."
    run = client.post("/api/runs", headers=headers, json={"project_id": project["id"], "dataset_version_id": detail["current_version_id"], "name": "linked run"}).json()
    refreshed = client.get(f"/api/datasets/{dataset['id']}").json()
    assert refreshed["training_runs"] == [{"id": run["id"], "name": "linked run", "status": "unknown", "dataset_version_id": detail["current_version_id"]}]


def test_source_update_is_scoped_to_selected_workspace(client: TestClient):
    source = client.post(
        "/api/import-sources",
        json={"name": "Primary source", "bucket": "bucket"},
    ).json()
    other = client.post("/api/workspaces", json={"name": "Other workspace"}).json()

    response = client.patch(
        f"/api/import-sources/{source['id']}",
        headers={"X-Workspace-ID": other["id"]},
        json={"name": "Wrong workspace mutation"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "import source not found"
    assert client.get("/api/import-sources").json()[0]["name"] == "Primary source"


    assert client.delete("/api/import-sources/not-a-source").status_code == 404
def test_source_disconnect_hides_configuration_and_preserves_references(client: TestClient):
    source = client.post("/api/import-sources", json={"name": "Disposable", "bucket": "bucket"}).json()
    assert client.delete(f"/api/import-sources/{source['id']}").status_code == 204
    assert source["id"] not in {row["id"] for row in client.get("/api/import-sources").json()}
    with app_module.SessionLocal() as db:
        assert db.get(models.ImportSource, source["id"]).is_active is False

    referenced = client.post("/api/import-sources", json={"name": "Referenced", "bucket": "bucket"}).json()
    with app_module.SessionLocal.begin() as db:
        workspace = db.query(models.Workspace).first()
        asset = models.Asset(workspace_id=workspace.id, kind=models.AssetKind.image, name="remote.png")
        db.add(asset); db.flush()
        db.add(models.AssetLocation(asset_id=asset.id, workspace_id=workspace.id, source_id=referenced["id"], provider="s3", uri="s3://bucket/remote.png", bucket="bucket", object_key="remote.png"))
    assert client.delete(f"/api/import-sources/{referenced['id']}").status_code == 204
    assert referenced["id"] not in {row["id"] for row in client.get("/api/import-sources").json()}
    with app_module.SessionLocal() as db:
        assert db.get(models.ImportSource, referenced["id"]).is_active is False
        assert db.query(models.AssetLocation).filter_by(source_id=referenced["id"]).one().source_id == referenced["id"]


def test_mega_connection_never_falls_through_to_an_s3_source(client: TestClient):
    client.post("/api/import-sources", json={"name": "AWS only", "bucket": "bucket", "credential_env_prefix": "S3"})
    response = client.post("/api/connections/mega/test")
    assert response.status_code == 409
    assert response.json()["detail"] == "no active MEGA import source is configured"


def test_source_connection_test_persists_canonical_identity_fingerprint(client: TestClient, monkeypatch):
    source = client.post(
        "/api/import-sources",
        json={
            "name": "Verified training source",
            "bucket": "training",
            "allowed_prefixes": ["titles-dam/managed/"],
            "credential_env_prefix": "TRAINING_S3",
        },
    ).json()

    class Browser:
        class settings:
            bucket = "training"
            allowed_prefixes = ["titles-dam/managed/"]

        def browse(self, prefix, page_size):
            assert prefix == "titles-dam/managed/"
            assert page_size == 1

    monkeypatch.setattr(integrations, "_browser", lambda _db, _source_id: Browser())
    with app_module.SessionLocal.begin() as db:
        db.get(models.ImportSource, source["id"]).identity_fingerprint = None

    response = client.post(f"/api/import-sources/{source['id']}/test")

    assert response.status_code == 200
    with app_module.SessionLocal() as db:
        persisted = db.get(models.ImportSource, source["id"])
        assert persisted.identity_fingerprint is not None
        assert persisted.identity_fingerprint.startswith("source-v1:")


def test_gallery_uses_canonical_origin_timestamps_and_no_filename_model_inference(client: TestClient):
    headers, project = _project(client)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    with app_module.SessionLocal.begin() as db:
        run = models.TrainingRun(project_id=project["id"], name="run", status="completed")
        db.add(run); db.flush()
        sample_asset = models.Asset(workspace_id=db.query(models.Workspace).first().id, project_id=project["id"], kind=models.AssetKind.image, name="looks-like-model-step-999.png", metadata_={"category": "sample"})
        db.add(sample_asset); db.flush()
        db.add(models.Sample(run_id=run.id, asset_id=sample_asset.id, step=999, modified_at=old))
        prompts = models.PromptSet(project_id=project["id"], name="prompts"); db.add(prompts); db.flush()
        definition = models.EvalDefinition(project_id=project["id"], name="eval", endpoint="test", prompt_set_id=prompts.id); db.add(definition); db.flush()
        eval_run = models.EvalRun(definition_id=definition.id); db.add(eval_run); db.flush()
        eval_asset = models.Asset(workspace_id=sample_asset.workspace_id, project_id=project["id"], kind=models.AssetKind.image, name="eval.png", metadata_={"category": "eval_output"}); db.add(eval_asset); db.flush()
        db.add(models.EvalOutput(eval_run_id=eval_run.id, asset_id=eval_asset.id, generated_at=recent))
    gallery = client.get("/api/gallery", params={"project_id": project["id"], "paginated": True}).json()["items"]
    assert [row["origin_type"] for row in gallery[:2]] == ["EVAL", "SAMPLE"]
    assert gallery[0]["generated_at"] is not None and gallery[0]["modified_at"] is None
    assert gallery[1]["modified_at"] is not None and gallery[1]["generated_at"] is None
    assert gallery[1]["model_id"] is None
