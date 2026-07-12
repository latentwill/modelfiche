from fastapi.testclient import TestClient
from titles_api import app as app_module, models


def test_run_metrics_endpoint_returns_sorted_loss_points(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Metrics"}).json()
    run = client.post("/api/runs", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "run"}).json()
    with app_module.SessionLocal() as session:
        session.add_all([
            models.TrainingMetric(run_id=run["id"], step=2, name="loss", value=0.5),
            models.TrainingMetric(run_id=run["id"], step=1, name="loss", value=1.0),
            models.TrainingMetric(run_id=run["id"], step=1, name="learning_rate", value=0.001),
        ])
        session.commit()

    response = client.get(f"/api/runs/{run['id']}/metrics", params={"name": "loss"})

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run["id"],
        "metric_name": "loss",
        "points": [
            {"step": 1, "value": 1.0, "wall_time": None},
            {"step": 2, "value": 0.5, "wall_time": None},
        ],
        "final_value": 0.5,
        "source_key": None,
    }


def test_checkpoint_and_model_version_establish_revision_for_fal_admission(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Revision"}).json()
    run = client.post("/api/runs", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "run"}).json()
    asset = client.post("/api/assets", json={"project_id": project["id"], "kind": "model", "name": "model.safetensors"}).json()
    with app_module.SessionLocal.begin() as session:
        workspace = session.get(models.Project, project["id"]).workspace_id
        session.add(models.AssetLocation(asset_id=asset["id"], workspace_id=workspace, provider="s3", uri="s3://bucket/model.safetensors", bucket="bucket", object_key="model.safetensors", version_id="v1", verification_state="available", hydration_state="remote"))
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", headers={"X-Profile-ID": profile["id"]}, json={"asset_id": asset["id"], "step": 100, "state": "available"}).json()
    model = client.post("/api/models", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project["id"], "name": "model"}).json()
    version = client.post("/api/model-versions", headers={"X-Profile-ID": profile["id"]}, json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "v1"}).json()

    detail = client.get(f"/api/model-versions/{version['id']}").json()
    assert detail["checkpoint_revision_id"]
    with app_module.SessionLocal() as session:
        stored_checkpoint = session.get(models.Checkpoint, checkpoint["id"])
        assert stored_checkpoint.current_revision_id == detail["checkpoint_revision_id"]


def test_project_dataset_draft_and_profile_attribution(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Test", "trigger_words": ["tok"]}).json()
    asset = client.post("/api/assets", json={"project_id": project["id"], "kind": "image", "name": "one.png"}).json()
    dataset = client.post("/api/datasets", json={"project_id": project["id"], "name": "Source", "items": [{"asset_id": asset["id"], "caption": "portrait"}]}).json()
    version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    draft = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version["id"]}).json()
    preview = client.post(f"/api/dataset-drafts/{draft['id']}/operations/preview", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True}).json()
    assert preview["changed"] == 1
    applied = client.post(f"/api/dataset-drafts/{draft['id']}/operations", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True}).json()
    assert applied["changed"] == 1
    published = client.post(f"/api/dataset-drafts/{draft['id']}/publish", json={"name": "Captioned"}).json()
    assert published["version_number"] == 2
    events = client.get("/api/activity", params={"project_id": project["id"]}).json()
    assert any(event["profile_id"] == profile["id"] for event in events)


def test_projects_hide_archived_records_unless_explicitly_requested(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    active = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Active"}).json()
    archived = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Smoke"}).json()
    client.patch(f"/api/projects/{archived['id']}", headers={"X-Profile-ID": profile["id"]}, json={"state": "archived"})

    default_ids = {row["id"] for row in client.get("/api/projects").json()}
    all_ids = {row["id"] for row in client.get("/api/projects", params={"include_archived": True}).json()}
    assert active["id"] in default_ids
    assert archived["id"] not in default_ids
    assert {active["id"], archived["id"]} <= all_ids
