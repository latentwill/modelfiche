import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from titles_api import app as app_module, models
from titles_api.routers import local_imports
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


    assert client.delete("/api/import-sources/not-a-source").status_code == 404
def test_source_delete_persists_and_referenced_source_is_safe(client: TestClient):
    source = client.post("/api/import-sources", json={"name": "Disposable", "bucket": "bucket"}).json()
    assert client.delete(f"/api/import-sources/{source['id']}").status_code == 204
    assert source["id"] not in {row["id"] for row in client.get("/api/import-sources").json()}

    referenced = client.post("/api/import-sources", json={"name": "Referenced", "bucket": "bucket"}).json()
    with app_module.SessionLocal.begin() as db:
        workspace = db.query(models.Workspace).first()
        asset = models.Asset(workspace_id=workspace.id, kind=models.AssetKind.image, name="remote.png")
        db.add(asset); db.flush()
        db.add(models.AssetLocation(asset_id=asset.id, workspace_id=workspace.id, source_id=referenced["id"], provider="s3", uri="s3://bucket/remote.png", bucket="bucket", object_key="remote.png"))
    blocked = client.delete(f"/api/import-sources/{referenced['id']}")
    assert blocked.status_code == 409
    assert "asset locations" in blocked.json()["detail"]


def test_mega_connection_never_falls_through_to_an_s3_source(client: TestClient):
    client.post("/api/import-sources", json={"name": "AWS only", "bucket": "bucket", "credential_env_prefix": "S3"})
    response = client.post("/api/connections/mega/test")
    assert response.status_code == 409
    assert response.json()["detail"] == "no active MEGA import source is configured"


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
