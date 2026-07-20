import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from titles_api import app as app_module, models
from titles_api.integrations.llm import PromptGenerator
from titles_api.routers import datasets
from titles_api.settings import get_settings


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
    item_page = client.get(
        f"/api/dataset-versions/{version['id']}/items",
        params={"limit": 1, "offset": 0, "paginated": True},
    ).json()
    assert item_page["total"] == 1
    assert len(item_page["items"]) == 1
    assert item_page["limit"] == 1
    assert item_page["offset"] == 0
    draft = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version["id"]}).json()
    preview = client.post(f"/api/dataset-drafts/{draft['id']}/operations/preview", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True}).json()
    assert preview["changed"] == 1
    applied = client.post(f"/api/dataset-drafts/{draft['id']}/operations", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True, "preview_token": preview["preview_token"]}).json()

    assert applied["changed"] == 1
    history = client.get(f"/api/dataset-drafts/{draft['id']}/operations").json()
    assert history[0]["operation"] == "add_word"
    assert history[0]["can_undo"] is True
    undone = client.post(f"/api/dataset-drafts/{draft['id']}/operations/{history[0]['id']}/undo")
    assert undone.status_code == 200
    assert client.get(f"/api/dataset-drafts/{draft['id']}").json()["items"][0]["caption"] == "portrait"
    preview = client.post(f"/api/dataset-drafts/{draft['id']}/operations/preview", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True}).json()
    assert client.post(f"/api/dataset-drafts/{draft['id']}/operations", json={"operation": "add_word", "parameters": {"word": "tok"}, "all": True, "preview_token": preview["preview_token"]}).status_code == 200
    published = client.post(f"/api/dataset-drafts/{draft['id']}/publish", json={"name": "Captioned"}).json()
    assert published["version_number"] == 2
    events = client.get("/api/activity", params={"project_id": project["id"]}).json()
    assert any(event["profile_id"] == profile["id"] for event in events)


def test_dataset_draft_caption_generation_updates_only_draft(client: TestClient, monkeypatch):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Caption"}).json()
    path = Path(get_settings().asset_root) / "caption.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-image")
    asset = client.post("/api/assets", json={
        "project_id": project["id"], "kind": "image", "name": "caption.png", "mime_type": "image/png",
        "location": {"provider": "local", "uri": str(path), "hydration_state": "hydrated"},
    }).json()
    dataset = client.post("/api/datasets", json={"project_id": project["id"], "name": "Source", "items": [{"asset_id": asset["id"], "caption": "old"}]}).json()
    version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    draft = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version["id"]}).json()
    client.patch("/api/operator-settings", json={"llm": {"provider": "openai", "model": "vision-model"}})
    requests = []

    def respond(request: httpx.Request):
        payload = json.loads(request.content)
        requests.append(payload)
        prompt = payload["messages"][0]["content"][0]["text"]
        content = '{"caption":"a generated caption"}' if "Return only a valid JSON object" in prompt else "a generated caption"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(datasets, "PromptGenerator", lambda: PromptGenerator(httpx.MockTransport(respond)))
    response = client.post(f"/api/dataset-drafts/{draft['id']}/caption", headers={"X-Profile-ID": profile["id"]}, json={"prompt": "Describe this image", "all": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["changed"] == 1
    assert payload["count"] == 1
    assert payload["items"][0]["caption"] == "a generated caption"
    assert payload["operation_id"]
    assert requests[0]["messages"][0]["content"][0]["text"] == "Describe this image"
    assert requests[0]["model"] == "vision-model"
    draft_item = client.get(f"/api/dataset-drafts/{draft['id']}").json()["items"][0]
    assert client.patch(f"/api/dataset-drafts/{draft['id']}/caption-format", headers={"X-Profile-ID": profile["id"]}, json={"caption_format": "json"}).status_code == 422
    assert client.patch(f"/api/dataset-drafts/{draft['id']}/items/{draft_item['id']}", json={"caption": '{"description":"existing caption"}'}).status_code == 200
    assert client.patch(f"/api/dataset-drafts/{draft['id']}/caption-format", headers={"X-Profile-ID": profile["id"]}, json={"caption_format": "json"}).json()["caption_format"] == "json"
    json_caption = client.post(f"/api/dataset-drafts/{draft['id']}/caption", headers={"X-Profile-ID": profile["id"]}, json={"prompt": "Describe this image", "model": "override-vision-model", "caption_format": "json", "all": True})
    assert json_caption.status_code == 200
    assert json_caption.json()["items"][0]["caption"] == '{"caption":"a generated caption"}'
    assert requests[-1]["model"] == "override-vision-model"
    assert "Return only a valid JSON object" in requests[-1]["messages"][0]["content"][0]["text"]
    client.patch("/api/operator-settings", json={"llm": {"provider": "openai", "model": ""}})
    missing_model = client.post(f"/api/dataset-drafts/{draft['id']}/caption", json={"prompt": "Describe", "all": True})
    assert missing_model.status_code == 422
    assert "model is required" in missing_model.json()["detail"]
    assert client.post(f"/api/dataset-drafts/{draft['id']}/caption", json={"prompt": "Describe"}).status_code == 422
    assert requests[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert client.get(f"/api/dataset-drafts/{draft['id']}").json()["state"] == "active"



def test_dataset_operation_preview_rejects_any_stale_draft_or_scope(client: TestClient):
    project = client.post("/api/projects", json={"title": "Preview integrity"}).json()
    assets = [
        client.post("/api/assets", json={"project_id": project["id"], "kind": "image", "name": f"{index}.png"}).json()
        for index in range(2)
    ]
    dataset = client.post("/api/datasets", json={
        "project_id": project["id"],
        "name": "Source",
        "items": [{"asset_id": asset["id"], "caption": f"caption {index}", "position": index} for index, asset in enumerate(assets)],
    }).json()
    version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    draft = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version["id"]}).json()
    items = client.get(f"/api/dataset-drafts/{draft['id']}").json()["items"]
    request = {"operation": "replace", "parameters": {"find": "caption", "replace": "image"}, "item_ids": [items[0]["id"]]}
    preview = client.post(f"/api/dataset-drafts/{draft['id']}/operations/preview", json=request).json()

    assert preview["target_item_ids"] == [items[0]["id"]]
    assert client.patch(f"/api/dataset-drafts/{draft['id']}/items/{items[1]['id']}", json={"included": False}).status_code == 200
    stale = client.post(f"/api/dataset-drafts/{draft['id']}/operations", json={**request, "preview_token": preview["preview_token"]})
    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]
    missing = client.post(f"/api/dataset-drafts/{draft['id']}/operations", json=request)
    assert missing.status_code == 409
    assert "preview required" in missing.json()["detail"]
    assert client.patch(
        f"/api/dataset-drafts/{draft['id']}/items/{items[0]['id']}",
        json={"caption": '{"description":"caption 0"}', "caption_format": "json"},
    ).status_code == 200
    invalid_json_edit = client.post(
        f"/api/dataset-drafts/{draft['id']}/operations/preview",
        json={"operation": "add_word", "parameters": {"word": "tok"}, "item_ids": [items[0]["id"]]},
    )
    assert invalid_json_edit.status_code == 422
    assert "would make JSON caption invalid" in invalid_json_edit.json()["detail"]


def test_dataset_active_draft_restores_working_copy(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Draft recovery"}).json()
    asset = client.post("/api/assets", json={"project_id": project["id"], "kind": "image", "name": "one.png"}).json()
    dataset = client.post("/api/datasets", json={"project_id": project["id"], "name": "Source", "items": [{"asset_id": asset["id"], "caption": "original"}]}).json()
    version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    draft = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version["id"]}).json()
    item = client.get(f"/api/dataset-drafts/{draft['id']}").json()["items"][0]
    client.patch(
        f"/api/dataset-drafts/{draft['id']}/items/{item['id']}",
        json={"caption": "working copy"},
    )

    restored = client.get(f"/api/datasets/{dataset['id']}/drafts/active")

    assert restored.status_code == 200
    assert restored.json()["id"] == draft["id"]
    assert restored.json()["items"][0]["caption"] == "working copy"
    assert client.get("/api/datasets/not-a-dataset/drafts/active").status_code == 404


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
