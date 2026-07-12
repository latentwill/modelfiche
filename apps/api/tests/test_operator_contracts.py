from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from titles_api import app as app_module, models
from titles_api.integrations.s3.detection import DetectionResult, PrefixKind
from titles_api.integrations.s3.importer import ImportContext, ImportResult
from titles_api.integrations.s3.models import ObjectInfo
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink


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


def test_gallery_opt_in_pagination_reports_total(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    for index in range(3):
        client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "image", "name": f"sample-{index}.png"})

    first = client.get("/api/gallery", params={"project_id": project["id"], "include_dataset_assets": True, "paginated": True, "limit": 2}).json()
    second = client.get("/api/gallery", params={"project_id": project["id"], "include_dataset_assets": True, "paginated": True, "limit": 2, "offset": 2}).json()

    assert first["total"] == 4
    assert first["limit"] == 2
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2


def test_gallery_context_uses_full_filtered_order_not_visible_page(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    created = []
    for index in range(4):
        response = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "image", "name": f"eval-{index}.png", "metadata": {"category": "eval_output"}})
        created.append(response.json())

    first_page = client.get("/api/gallery", params={"project_id": project["id"], "category": "eval_output", "kind": "image", "paginated": True, "limit": 1}).json()
    assert first_page["total"] == 4

    context = client.get("/api/gallery/context", params={"project_id": project["id"], "category": "eval_output", "kind": "image", "asset_id": created[1]["id"]}).json()
    assert context["total"] == 4
    assert context["current"]["id"] == created[1]["id"]
    assert context["previous"]["id"] != context["current"]["id"]
    assert context["next"]["id"] != context["current"]["id"]


def test_gallery_model_filter_uses_run_lineage_and_returns_rich_metadata(client: TestClient):
    _profile, headers, project, _asset, dataset = _seed(client)
    dataset_detail = client.get(f"/api/datasets/{dataset['id']}").json()
    run = client.post("/api/runs", headers=headers, json={"project_id": project["id"], "dataset_version_id": dataset_detail["current_version_id"], "name": "Qwen run", "trainer": "ai-toolkit", "base_model": "qwen-image", "normalized_config": {"rank": 16}}).json()
    checkpoint_asset = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "model", "name": "step-250.safetensors"}).json()
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", headers=headers, json={"step": 250, "asset_id": checkpoint_asset["id"]}).json()
    model = client.post("/api/models", headers=headers, json={"project_id": project["id"], "name": "Portrait LoRA"}).json()
    client.post("/api/model-versions", headers=headers, json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "step 250", "base_model": "qwen-image"})
    sample_asset = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "image", "name": "sample.png"}).json()
    client.post(f"/api/runs/{run['id']}/samples", json={"asset_id": sample_asset["id"], "step": 250, "prompt": "a portrait", "seed": 42, "generation_metadata": {"guidance": 3.5}})

    gallery = client.get("/api/gallery", params={"model_id": model["id"], "paginated": True}).json()

    assert gallery["total"] == 1
    row = gallery["items"][0]
    assert row["id"] == sample_asset["id"]
    assert row["model_name"] == "Portrait LoRA"
    assert row["base_model"] == "qwen-image"
    assert row["dataset_name"] == "Faces"
    assert row["metadata"]["generation_settings"] == {"guidance": 3.5}
    assert row["metadata"]["training_settings"] == {"rank": 16}


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


def test_import_source_does_not_require_allowed_prefixes(client: TestClient):
    source = client.post(
        "/api/import-sources",
        json={"name": "Whole bucket", "bucket": "titlesxyz", "credential_env_prefix": "MEGA"},
    )

    assert source.status_code == 201
    assert source.json()["allowed_prefixes"] == []


def test_s3_dataset_import_hydrates_existing_referenced_dataset(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "kzapata"}).json()

    with app_module.SessionLocal() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="Dataset archive",
            provider="s3",
            bucket="titlesxyz",
            allowed_prefixes=["title-lora/datasets/"],
            is_active=True,
        )
        dataset = models.Dataset(project_id=project["id"], name="kzapata-krea2-captions-v002")
        db.add_all([source, dataset])
        db.flush()
        version = models.DatasetVersion(
            dataset_id=dataset.id,
            version_number=1,
            name=dataset.name,
            source_uri="training-path:///app/ai-toolkit/datasets/kzapata-krea2-captions-v002",
            caption_format="text",
            status="referenced",
        )
        db.add(version)
        db.commit()
        source_id = source.id
        version_id = version.id

    prefix = "title-lora/datasets/kzapata/krea2-captions-v002/"
    with app_module.SessionLocal() as db:
        sink = SQLAlchemyImportSink(db)
        sink.upsert_remote_object(
            source_id,
            ObjectInfo(
                key=f"{prefix}01.png",
                size=123,
                etag="etag-01",
                modified_at=datetime.now(timezone.utc),
            ),
            category="dataset_image",
        )
        sink.save_source_snapshot(source_id, f"{prefix}01.txt", b"kzapata portrait")
        sink.finalize_import(
            ImportContext(source_id=source_id, project_id=project["id"], prefix=prefix, requested_by_profile_id=profile["id"]),
            ImportResult(
                detection=DetectionResult(
                    kind=PrefixKind.DATASET,
                    confidence=1,
                    signals=("1 image-caption pairs",),
                    warnings=(),
                    observed={"objects": 2, "history_objects": 0, "images": 1, "caption_pairs": 1, "checkpoints": 0, "samples": 0},
                ),
                indexed=1,
                changed=0,
                unchanged=0,
                remote_missing=0,
                small_sources_saved=1,
                warnings=(),
            ),
        )
        hydrated = db.get(models.DatasetVersion, version_id)
        item_count = db.scalar(select(func.count(models.DatasetItem.id)).where(models.DatasetItem.dataset_version_id == version_id))
        assert hydrated.status == "published"
        assert hydrated.source_uri == "s3://titlesxyz/title-lora/datasets/kzapata/krea2-captions-v002"
        assert item_count == 1
