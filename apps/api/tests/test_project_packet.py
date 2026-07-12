from fastapi.testclient import TestClient


def _profile_and_project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post(
        "/api/projects",
        headers={"X-Profile-ID": profile["id"]},
        json={"title": "vcribb"},
    ).json()
    return profile, project


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
