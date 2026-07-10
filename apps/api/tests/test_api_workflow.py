from fastapi.testclient import TestClient


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
