from fastapi.testclient import TestClient


def _seed(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Portrait project"}).json()
    asset = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "image", "name": "portrait.png"}).json()
    dataset = client.post("/api/datasets", headers=headers, json={"project_id": project["id"], "name": "Faces", "items": [{"asset_id": asset["id"], "caption": "portrait"}]}).json()
    return profile, headers, project, asset, dataset


def test_wireframe_dashboard_search_and_enriched_lists(client: TestClient):
    profile, headers, project, asset, dataset = _seed(client)

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["counts"]["projects"] == 1
    assert dashboard["counts"]["datasets"] == 1
    assert dashboard["recent_activity"][0]["profile_name"] == profile["display_name"]

    search = client.get("/api/search", params={"q": "portrait"}).json()
    assert {(item["type"], item["title"]) for item in search["results"]} >= {("project", "Portrait project"), ("asset", "portrait.png")}

    dataset_row = client.get("/api/datasets", params={"project_id": project["id"]}).json()[0]
    assert dataset_row["project_name"] == "Portrait project"
    assert dataset_row["current_version"] == 1
    assert dataset_row["item_count"] == 1

    detail = client.get(f"/api/datasets/{dataset['id']}").json()
    items = client.get(f"/api/dataset-versions/{detail['current_version_id']}/items").json()
    assert items[0]["filename"] == "portrait.png"
    version = client.get(f"/api/dataset-versions/{detail['current_version_id']}").json()
    assert version["dataset_name"] == "Faces"
    assert version["project_id"] == project["id"]
    assert version["item_count"] == 1


def test_gallery_filters_and_attributed_collaboration_context(client: TestClient):
    profile, headers, project, asset, _dataset = _seed(client)
    subject = {"subject_type": "asset", "subject_id": asset["id"]}
    review = client.post("/api/reviews", headers=headers, json={**subject, "rating": 5, "decision": "approved"})
    assert review.status_code == 201
    assert review.json()["profile_name"] == profile["display_name"]
    client.post("/api/comments", headers=headers, json={**subject, "body": "Use this one"})

    gallery = client.get("/api/gallery", params={"project_id": project["id"], "decision": "approved", "rating": 5, "include_dataset_assets": True}).json()
    assert len(gallery) == 1
    assert gallery[0]["id"] == asset["id"]
    assert gallery[0]["review_count"] == 1
    assert gallery[0]["comment_count"] == 1

    assert client.get("/api/gallery", params={"decision": "reject"}).json() == []
    context = client.get(f"/api/assets/{asset['id']}/context").json()
    assert context["comments"][0]["profile_name"] == profile["display_name"]
    assert context["reviews"][0]["decision"] == "approved"

    activity = client.get("/api/activity", params={"project_id": project["id"]}).json()
    assert all("event_type" in row and "summary" in row and "profile_name" in row for row in activity)


def test_fal_endpoint_schema_is_adapter_driven(client: TestClient):
    endpoints = client.get("/api/eval-endpoints").json()
    ideogram = next(row for row in endpoints if row["endpoint_id"] == "ideogram/v4/lora")
    assert ideogram["name"] == "Ideogram 4 LoRA"
    assert ideogram["fields"]["rendering_speed"]["options"] == ["TURBO", "BALANCED", "QUALITY"]
    assert ideogram["fields"]["image_size"]["custom"] == {"min": 512, "max": 3840, "step": 16}

    krea = client.get("/api/eval-endpoints/fal-ai/krea-2/turbo/lora/schema").json()
    assert krea["name"] == "Krea 2 Turbo LoRA"
    assert krea["fields"]["acceleration"]["options"] == ["none", "regular"]


def test_profile_archive_preserves_active_attribution(client: TestClient):
    initial = client.get("/api/profiles/active").json()
    assert client.post(f"/api/profiles/{initial['id']}/archive").status_code == 409

    second = client.post("/api/profiles", json={"display_name": "Reviewer"}).json()
    assert client.put(f"/api/profiles/{second['id']}/active").status_code == 200
    archived = client.post(f"/api/profiles/{initial['id']}/archive", headers={"X-Profile-ID": second["id"]})
    assert archived.status_code == 200
    assert client.get("/api/profiles/active").json()["id"] == second["id"]
