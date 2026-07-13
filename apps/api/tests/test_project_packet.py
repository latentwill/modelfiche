from fastapi.testclient import TestClient
from titles_api import app as app_module, models

def _profile_and_project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "vcribb"},
    ).json()
    return profile, project


def test_activity_and_recent_runs_expose_safe_object_enrichment(client: TestClient):
    profile, project = _profile_and_project(client)
    image = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "preview.png", "mime_type": "image/png"},
    ).json()
    model_asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "model", "name": "checkpoint.safetensors"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        json={"project_id": project["id"], "name": "Training set", "items": [{"asset_id": image["id"]}]},
    ).json()
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "Training run"}).json()
    assert client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": image["id"], "step": 10}).status_code == 201

    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        session.add(
            models.ActivityEvent(
                workspace_id=workspace_id,
                project_id=project["id"],
                profile_id=profile["id"],
                action="test.unknown",
                subject_type="not_routable",
                subject_id="missing",
                details={},
            )
        )

    events = client.get("/api/activity", params={"project_id": project["id"]}).json()
    by_subject = {(event["subject_type"], event["subject_id"]): event for event in events}
    assert by_subject[("project", project["id"])]["object_href"] == f"#/project/{project['id']}"
    assert by_subject[("project", project["id"])]["object_label"] == "vcribb"
    assert by_subject[("dataset", dataset["id"])]["object_href"] == f"#/dataset/{dataset['id']}"
    assert by_subject[("dataset", dataset["id"])]["preview_asset_id"] == image["id"]
    assert by_subject[("asset", image["id"])]["object_href"] == f"#/image/{image['id']}"
    assert by_subject[("asset", image["id"])]["preview_asset_id"] == image["id"]
    assert by_subject[("asset", model_asset["id"])]["object_href"] is None
    assert by_subject[("asset", model_asset["id"])]["preview_asset_id"] is None
    assert by_subject[("not_routable", "missing")]["object_href"] is None
    assert by_subject[("not_routable", "missing")]["object_label"] is None
    assert by_subject[("not_routable", "missing")]["preview_asset_id"] is None

    listed = client.get("/api/runs", params={"project_id": project["id"]}).json()[0]
    assert listed["preview_asset_id"] == image["id"]


def test_workspace_rename_persists_and_is_returned_without_cache_delay(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    workspace = client.get("/api/workspaces").json()[0]

    response = client.patch(
        f"/api/workspaces/{workspace['id']}",
        headers={"X-Profile-ID": profile["id"]},
        json={"name": "TitlesXYZ"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "TitlesXYZ"
    assert client.get("/api/workspaces").json()[0]["name"] == "TitlesXYZ"


def test_training_sample_gallery_uses_sample_membership_not_legacy_metadata(client: TestClient):
    _, project = _profile_and_project(client)
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "train"}).json()
    asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "sample.png", "mime_type": "image/png", "metadata": {}},
    ).json()
    created = client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": asset["id"], "step": 100})
    assert created.status_code == 201

    gallery = client.get(
        "/api/gallery",
        params={"project_id": project["id"], "kind": "image", "category": "sample"},
    ).json()

    assert [row["id"] for row in gallery] == [asset["id"]]
    assert gallery[0]["origin_type"] == "SAMPLE"
    assert gallery[0]["generated_at"] is None

def test_training_samples_are_assigned_to_and_listed_only_for_their_run_project(client: TestClient):
    _, modern_times = _profile_and_project(client)
    vcribb = client.post("/api/projects", json={"title": "vcribb"}).json()
    run = client.post("/api/runs", json={"project_id": vcribb["id"], "name": "train"}).json()
    unassigned_asset = client.post(
        "/api/assets",
        json={"kind": "image", "name": "sample.png", "mime_type": "image/png", "metadata": {}},
    ).json()

    created = client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": unassigned_asset["id"], "step": 100})

    assert created.status_code == 201
    assert client.get(f"/api/assets/{unassigned_asset['id']}").json()["project_id"] == vcribb["id"]
    vcribb_samples = client.get(
        "/api/gallery",
        params={"project_id": vcribb["id"], "kind": "image", "category": "sample"},
    ).json()
    modern_times_samples = client.get(
        "/api/gallery",
        params={"project_id": modern_times["id"], "kind": "image", "category": "sample"},
    ).json()
    assert [row["id"] for row in vcribb_samples] == [unassigned_asset["id"]]
    assert modern_times_samples == []


def test_training_sample_cannot_be_attached_to_a_run_in_another_project(client: TestClient):
    _, modern_times = _profile_and_project(client)
    vcribb = client.post("/api/projects", json={"title": "vcribb"}).json()
    run = client.post("/api/runs", json={"project_id": vcribb["id"], "name": "train"}).json()
    modern_times_asset = client.post(
        "/api/assets",
        json={"project_id": modern_times["id"], "kind": "image", "name": "sample.png", "mime_type": "image/png", "metadata": {}},
    ).json()

    response = client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": modern_times_asset["id"], "step": 100})

    assert response.status_code == 409
    assert response.json()["detail"] == "sample asset belongs to another project"


def test_project_runs_expose_correct_result_model_route_id(client: TestClient):
    _, project = _profile_and_project(client)
    run = client.post("/api/runs", json={"project_id": project["id"], "name": "train", "status": "completed"}).json()
    asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "model", "name": "result.safetensors"},
    ).json()
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", json={"asset_id": asset["id"], "step": 100}).json()
    model = client.post("/api/models", json={"project_id": project["id"], "name": "Result family"}).json()
    version = client.post(
        "/api/model-versions",
        json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "v1"},
    ).json()

    listed = client.get("/api/runs", params={"project_id": project["id"]}).json()[0]

    assert listed["id"] == run["id"]
    assert listed["result_model_id"] == model["id"]
    assert listed["result_model_name"] == "Result family"
    assert listed["result_model_version_id"] == version["id"]
