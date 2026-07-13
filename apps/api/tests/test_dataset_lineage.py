from fastapi.testclient import TestClient


def _source_dataset(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Lineage project"}).json()
    assets = [
        client.post(
            "/api/assets",
            headers=headers,
            json={"project_id": project["id"], "kind": "image", "name": "one.png"},
        ).json(),
        client.post(
            "/api/assets",
            headers=headers,
            json={"project_id": project["id"], "kind": "image", "name": "two.png"},
        ).json(),
    ]
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "Source dataset",
            "description": "source",
            "source_uri": "s3://bucket/source",
            "caption_format": "text",
            "trigger_words": ["tok"],
            "items": [
                {"asset_id": assets[0]["id"], "caption": "first", "tags": ["a"], "position": 2},
                {"asset_id": assets[1]["id"], "caption": "second", "included": False, "position": 1},
            ],
        },
    ).json()
    source_version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    return headers, project, assets, dataset, source_version


def test_duplicate_dataset_version_reuses_assets_and_can_open_and_publish_draft(client: TestClient):
    headers, project, assets, source_dataset, source_version = _source_dataset(client)

    assert source_version["lineage_kind"] == "initial"
    assert source_version["parent_version_id"] is None
    assert len(source_version["content_digest"]) == 64

    response = client.post(
        f"/api/dataset-versions/{source_version['id']}/duplicate",
        headers=headers,
        json={"name": "Copied dataset", "description": "copied", "open_draft": True},
    )
    assert response.status_code == 201
    result = response.json()
    assert set(result) == {"dataset_id", "current_version_id", "draft_id"}
    assert result["draft_id"]

    copied_dataset = client.get(f"/api/datasets/{result['dataset_id']}").json()
    assert copied_dataset["project_id"] == project["id"]
    assert copied_dataset["name"] == "Copied dataset"
    assert copied_dataset["description"] == "copied"

    copied_version = client.get(f"/api/dataset-versions/{result['current_version_id']}").json()
    assert copied_version["parent_version_id"] == source_version["id"]
    assert copied_version["lineage_kind"] == "dataset_duplicate"
    assert copied_version["content_digest"] == source_version["content_digest"]

    source_items = client.get(f"/api/dataset-versions/{source_version['id']}/items").json()
    copied_items = client.get(f"/api/dataset-versions/{copied_version['id']}/items").json()
    assert [item["asset_id"] for item in copied_items] == [item["asset_id"] for item in source_items]
    assert [{key: item[key] for key in ("caption", "caption_format", "included", "tags", "position")} for item in copied_items] == [
        {key: item[key] for key in ("caption", "caption_format", "included", "tags", "position")} for item in source_items
    ]
    assert {item["asset_id"] for item in copied_items} == {asset["id"] for asset in assets}

    draft = client.get(f"/api/dataset-drafts/{result['draft_id']}").json()
    assert draft["base_version_id"] == copied_version["id"]
    assert [item["asset_id"] for item in draft["items"]] == [item["asset_id"] for item in copied_items]

    patched = client.patch(
        f"/api/dataset-drafts/{result['draft_id']}/items/{draft['items'][0]['id']}",
        headers=headers,
        json={"caption": "published edit"},
    )
    assert patched.status_code == 200
    published = client.post(
        f"/api/dataset-drafts/{result['draft_id']}/publish",
        headers=headers,
        json={"name": "Edited version"},
    )
    assert published.status_code == 201
    published_version = published.json()
    assert published_version["parent_version_id"] == copied_version["id"]
    assert published_version["lineage_kind"] == "draft_publish"
    assert len(published_version["content_digest"]) == 64
    assert published_version["content_digest"] != copied_version["content_digest"]
