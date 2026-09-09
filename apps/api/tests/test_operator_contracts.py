import json

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



def test_gallery_metadata_contract_preserves_evidence_precedence_and_entities(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Metadata project"}).json()
    asset = client.post("/api/assets", headers=headers, json={
        "project_id": project["id"], "kind": "image", "name": "contract.png",
        "metadata": {
            "model_id": "explicit-model", "model_name": "Explicit model",
            "dataset_id": "explicit-dataset", "dataset_name": "Explicit dataset",
            "base_model": "Explicit base", "provider": "explicit-provider",
        },
    }).json()
    dataset = client.post("/api/datasets", headers=headers, json={
        "project_id": project["id"], "name": "Relational dataset",
        "items": [{"asset_id": asset["id"], "caption": "contract"}],
    })
    assert dataset.status_code == 201

    row = client.get("/api/gallery", params={"project_id": project["id"], "include_dataset_assets": True}).json()[0]
    metadata = row["metadata"]
    assert metadata["metadata_contract_version"] == 1
    assert metadata["evidence"]["model_id"] == "explicit"
    assert metadata["evidence"]["dataset_id"] == "explicit"
    assert metadata["evidence"]["asset_type"] == "canonical"
    assert metadata["relationships"]["model"] == {"id": "explicit-model", "name": "Explicit model", "evidence": "explicit"}
    assert metadata["relationships"]["dataset"]["name"] == "Explicit dataset"
    assert metadata["relationships"]["base_model"]["name"] == "Explicit base"
    assert metadata["relationships"]["provider"]["name"] == "explicit-provider"
    assert row["model_id"] == metadata["model_id"]
    assert row["dataset_id"] == metadata["dataset_id"]

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


def test_gallery_origin_sort_does_not_invent_eval_or_sample_timestamps(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    legacy_eval = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "image", "name": "legacy-eval.png", "metadata": {"category": "eval_output"}}).json()
    row = client.get("/api/gallery", params={"project_id": project["id"], "category": "eval_output", "include_dataset_assets": True}).json()[0]
    assert row["id"] == legacy_eval["id"]
    assert row["origin_type"] == "EVAL"
    assert row["generated_at"] is None
    assert row["modified_at"] is None


def test_gallery_delete_reports_retained_remote_content_and_removes_metadata(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    asset = client.post("/api/assets", headers=headers, json={
        "project_id": project["id"], "kind": "image", "name": "remote.png",
        "location": {"provider": "s3", "uri": "s3://bucket/remote.png", "bucket": "bucket", "object_key": "remote.png"},
    }).json()
    response = client.delete(f"/api/assets/{asset['id']}?report=true&delete_content=true", headers=headers)
    assert response.status_code == 200
    assert response.json()["metadata"] == "deleted"
    assert response.json()["storage"][0]["status"] == "retained"
    assert client.get(f"/api/assets/{asset['id']}").status_code == 404



def test_gallery_delete_clears_asset_location_references(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    asset = client.post("/api/assets", headers=headers, json={
        "project_id": project["id"],
        "kind": "image",
        "name": "generated.png",
        "location": {"provider": "s3", "uri": "s3://bucket/generated.png", "bucket": "bucket", "object_key": "generated.png"},
    }).json()
    with app_module.SessionLocal() as db:
        location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset["id"]))
        row = db.get(models.Asset, asset["id"])
        assert location is not None and row is not None
        row.preferred_location_id = location.id
        row.origin_location_id = location.id
        dependent = models.Asset(
            workspace_id=row.workspace_id,
            project_id=project["id"],
            kind=models.AssetKind.image,
            name="derived.png",
            preferred_location_id=location.id,
            origin_location_id=location.id,
        )
        db.add(dependent)
        db.commit()
        dependent_id = dependent.id

    response = client.delete(f"/api/assets/{asset['id']}?report=true&delete_content=true", headers=headers)

    assert response.status_code == 200
    assert response.json()["metadata"] == "deleted"
    assert client.get(f"/api/assets/{asset['id']}").status_code == 404
    with app_module.SessionLocal() as db:
        dependent = db.get(models.Asset, dependent_id)
        assert dependent is not None
        assert dependent.preferred_location_id is None
        assert dependent.origin_location_id is None

def test_gallery_delete_reports_partial_local_storage_failure(client: TestClient, tmp_path):
    _profile, headers, project, _asset, _dataset = _seed(client)
    outside = tmp_path / "outside-root.png"
    outside.write_bytes(b"not-an-image")
    asset = client.post("/api/assets", headers=headers, json={
        "project_id": project["id"], "kind": "image", "name": "outside.png",
        "location": {"provider": "local", "uri": str(outside), "hydration_state": "hydrated"},
    }).json()
    response = client.delete(f"/api/assets/{asset['id']}?report=true&delete_content=true", headers=headers)
    assert response.status_code == 207
    assert response.json()["metadata"] == "deleted"
    assert response.json()["storage"][0]["status"] == "failed"
    assert "outside configured storage roots" in response.json()["storage"][0]["detail"]
    assert outside.exists()


def test_gallery_refuses_dataset_source_deletion(client: TestClient):
    _profile, headers, _project, asset, _dataset = _seed(client)
    response = client.delete(f"/api/assets/{asset['id']}?report=true&delete_content=true", headers=headers)
    assert response.status_code == 409
    assert "dataset versioning" in response.json()["detail"]
    assert client.get(f"/api/assets/{asset['id']}").status_code == 200


def test_gallery_model_filter_uses_run_lineage_and_returns_rich_metadata(client: TestClient):
    _profile, headers, project, _asset, dataset = _seed(client)
    dataset_detail = client.get(f"/api/datasets/{dataset['id']}").json()
    run = client.post("/api/runs", headers=headers, json={"project_id": project["id"], "dataset_version_id": dataset_detail["current_version_id"], "name": "Qwen run", "trainer": "ai-toolkit", "base_model": "qwen-image", "normalized_config": {"rank": 16}}).json()
    checkpoint_asset = client.post("/api/assets", headers=headers, json={"project_id": project["id"], "kind": "model", "name": "step-250.safetensors"}).json()
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", headers=headers, json={"step": 250, "asset_id": checkpoint_asset["id"]}).json()
    model = client.post("/api/models", headers=headers, json={"project_id": project["id"], "name": "Portrait LoRA"}).json()
    version = client.post("/api/model-versions", headers=headers, json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "step 250", "base_model": "qwen-image"}).json()
    model_detail = client.get(f"/api/models/{model['id']}").json()
    artifact_version = model_detail["versions"][0]
    assert artifact_version["filename"] == "step-250.safetensors"
    assert artifact_version["asset_id"] == checkpoint_asset["id"]
    assert artifact_version["artifact"]["checkpoint_id"] == checkpoint["id"]
    assert artifact_version["artifact"]["step"] == 250
    assert artifact_version["storage"] == []
    version_detail = client.get(f"/api/model-versions/{version['id']}").json()
    assert version_detail["filename"] == "step-250.safetensors"
    assert version_detail["checkpoint_run_id"] == run["id"]
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
    assert row["metadata"]["relationships"]["model"]["name"] == "Portrait LoRA"
    assert row["metadata"]["relationships"]["checkpoint"]["id"] == checkpoint["id"]
    assert row["metadata"]["relationships"]["checkpoint"]["name"] == "step 250"
    assert row["metadata"]["relationships"]["dataset"]["name"] == "Faces"


def test_prompt_set_writes_are_removed(client: TestClient):
    _profile, headers, project, _asset, _dataset = _seed(client)
    response = client.post("/api/prompt-sets", headers=headers, json={"project_id": project["id"], "name": "Identity holdouts", "prompts": ["front portrait"]})
    assert response.status_code == 405

def test_fal_endpoint_schema_is_adapter_driven(client: TestClient):
    endpoints = client.get("/api/eval-endpoints").json()
    assert {row["endpoint_id"] for row in endpoints} == {
        "ideogram/v4/lora",
        "fal-ai/krea-2/turbo/lora",
        "fal-ai/z-image/turbo/lora",
        "fal-ai/flux-2/klein/9b/base/lora",
    }
    ideogram = next(row for row in endpoints if row["endpoint_id"] == "ideogram/v4/lora")
    assert ideogram["name"] == "Ideogram 4 LoRA"
    assert ideogram["fields"]["rendering_speed"]["options"] == ["TURBO", "BALANCED", "QUALITY"]
    assert ideogram["fields"]["image_size"]["custom"] == {"min": 512, "max": 3840, "step": 16}

    krea = client.get("/api/eval-endpoints/fal-ai/krea-2/turbo/lora/schema").json()
    assert krea["name"] == "Krea 2 Turbo LoRA"
    assert krea["fields"]["acceleration"]["options"] == ["none", "regular"]
    assert krea["fields"]["enable_prompt_expansion"]["type"] == "boolean"
    assert set(krea["grid"]["axis_fields"]) == {
        "prompt", "seed", "lora_scale",
        "image_size", "acceleration", "enable_prompt_expansion",
    }
    assert "guidance_scale" not in krea["grid"]["axis_fields"]
    assert "num_inference_steps" not in krea["grid"]["axis_fields"]
    assert krea["grid"]["axis_fields"]["seed"]["parser"] == "integer"
    assert krea["grid"]["axis_fields"]["lora_scale"]["min"] == 0
    assert krea["grid"]["axis_fields"]["prompt"]["max_length"] == 5000
    assert krea["grid"]["fixed_field_schema"]["image_size"]["custom"]["max"] == 14142

    z_image = next(row for row in endpoints if row["endpoint_id"] == "fal-ai/z-image/turbo/lora")
    assert z_image["defaults"]["image_size"] == "landscape_4_3"
    assert z_image["fields"]["num_inference_steps"] == {"type": "integer", "min": 1, "max": 8}
    assert z_image["fields"]["output_format"]["options"] == ["jpeg", "png", "webp"]

    klein = next(row for row in endpoints if row["endpoint_id"] == "fal-ai/flux-2/klein/9b/base/lora")
    assert klein["fields"]["guidance_scale"] == {"type": "number", "min": 0, "max": 20, "step": 0.1}
    assert klein["fields"]["negative_prompt"]["type"] == "string"
    assert klein["compatible_base_model_markers"] == ["klein"]

def test_grid_adapter_contract_reserves_model_comparison_for_target_axis(client: TestClient):
    endpoints = client.get("/api/eval-endpoints").json()
    for endpoint in endpoints:
        schema = client.get(f"/api/eval-endpoints/{endpoint['endpoint_id']}/schema").json()
        axes = set(schema["grid"]["axis_fields"])
        assert {"model", "checkpoint_step"}.isdisjoint(axes), endpoint["endpoint_id"]
        assert {"model", "checkpoint_step"}.isdisjoint(schema["grid"]["fixed_fields"])


def test_workspace_header_isolates_projects_models_assets_and_profiles(client: TestClient):
    primary_profile = client.get("/api/profiles").json()[0]
    primary_project = client.post("/api/projects", headers={"X-Profile-ID": primary_profile["id"]}, json={"title": "Titles project"}).json()
    client.post("/api/models", headers={"X-Profile-ID": primary_profile["id"]}, json={"project_id": primary_project["id"], "name": "Titles model"})
    client.post("/api/assets", headers={"X-Profile-ID": primary_profile["id"]}, json={"project_id": primary_project["id"], "kind": "image", "name": "titles.png"})

    with app_module.SessionLocal.begin() as db:
        personal = models.Workspace(name="Personal")
        db.add(personal)
        db.flush()
        personal_profile = models.UserProfile(workspace_id=personal.id, display_name="Personal operator", is_active=True)
        db.add(personal_profile)
        db.flush()
        personal_id = personal.id
        personal_profile_id = personal_profile.id

    personal_headers = {"X-Workspace-ID": personal_id, "X-Profile-ID": personal_profile_id}
    personal_project = client.post("/api/projects", headers=personal_headers, json={"title": "Hoover"}).json()
    client.post("/api/models", headers=personal_headers, json={"project_id": personal_project["id"], "name": "Hoover model"})
    client.post("/api/assets", headers=personal_headers, json={"project_id": personal_project["id"], "kind": "image", "name": "hoover.png"})

    assert [row["title"] for row in client.get("/api/projects", headers=personal_headers).json()] == ["Hoover"]
    assert [row["name"] for row in client.get("/api/models", headers=personal_headers).json()] == ["Hoover model"]
    assert [row["name"] for row in client.get("/api/assets", headers=personal_headers).json()] == ["hoover.png"]
    assert [row["display_name"] for row in client.get("/api/profiles", headers=personal_headers).json()] == ["Personal operator"]
    assert [row["title"] for row in client.get("/api/projects").json()] == ["Titles project"]
    assert client.get("/api/projects", headers={"X-Workspace-ID": "missing"}).status_code == 404


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

def test_training_import_promotes_resolved_settings_and_sample_provenance(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Lotus Rocks"}).json()
    prefix = "modelfiche-imports/lotus-v004/"
    now = datetime.now(timezone.utc)
    with app_module.SessionLocal() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="Imagesets",
            provider="s3",
            bucket="imagesets",
            allowed_prefixes=["modelfiche-imports/"],
            is_active=True,
        )
        db.add(source)
        db.flush()
        sink = SQLAlchemyImportSink(db)
        sink.upsert_remote_object(
            source.id,
            ObjectInfo(
                key=f"{prefix}checkpoints/embedding_step250.safetensors",
                size=10,
                etag="checkpoint",
                modified_at=now,
            ),
            category="checkpoint",
        )
        sink.upsert_remote_object(
            source.id,
            ObjectInfo(
                key=f"{prefix}samples/checkpoint-00250/building-style-strength1.0-seed3001.png",
                size=10,
                etag="sample",
                modified_at=now,
            ),
            category="sample",
        )
        sink.upsert_remote_object(
            source.id,
            ObjectInfo(
                key=f"{prefix}samples/checkpoint-00250/building-baseline-strength0.0-seed3001.png",
                size=10,
                etag="baseline",
                modified_at=now,
            ),
            category="sample",
        )
        sink.save_source_snapshot(
            source.id,
            f"{prefix}config.json",
            json.dumps(
                {
                    "training": {
                        "model_id": "krea/Krea-2-Raw",
                        "steps": 1500,
                        "learning_rate": 0.0001,
                    }
                }
            ).encode(),
        )
        sink.save_source_snapshot(
            source.id,
            f"{prefix}run_manifest.json",
            json.dumps({"name": "lotus-v004", "status": "completed"}).encode(),
        )
        sink.save_source_snapshot(
            source.id,
            f"{prefix}run_state.json",
            json.dumps({"status": "success"}).encode(),
        )
        sink.save_source_snapshot(
            source.id,
            f"{prefix}samples/checkpoint-00250/manifest.json",
            json.dumps(
                {
                    "step": 250,
                    "samples": [
                        {
                            "path": "building-style-strength1.0-seed3001.png",
                            "prompt": "a glass office building in a city plaza",
                            "seed": 3001,
                            "sample_track": "building",
                            "role": "style",
                            "strength": 1.0,
                            "negative_prompt": "",
                            "sha256": "sample-sha",
                        },
                        {
                            "path": "building-baseline-strength0.0-seed3001.png",
                            "prompt": "a glass office building in a city plaza",
                            "seed": 3001,
                            "sample_track": "building",
                            "role": "baseline",
                            "strength": 0.0,
                        },
                    ],
                }
            ).encode(),
        )
        sink.finalize_import(
            ImportContext(
                source_id=source.id,
                project_id=project["id"],
                prefix=prefix,
                requested_by_profile_id=profile["id"],
            ),
            ImportResult(
                detection=DetectionResult(
                    kind=PrefixKind.TRAINING_RUN,
                    confidence=1,
                    signals=(),
                    warnings=(),
                    observed={},
                    metadata={"manifest": {"name": "lotus-v004", "status": "completed"}},
                ),
                indexed=2,
                changed=0,
                unchanged=0,
                remote_missing=0,
                small_sources_saved=3,
                warnings=(),
            ),
        )
        db.commit()
        run = db.scalar(
            select(models.TrainingRun).where(
                models.TrainingRun.origin_source_id == source.id,
                models.TrainingRun.source_prefix == prefix,
            )
        )
        assert run is not None
        samples = list(db.scalars(select(models.Sample).where(models.Sample.run_id == run.id)))
        assert run.normalized_config["steps"] == 1500
        assert run.normalized_config["learning_rate"] == 0.0001
        assert len(samples) == 1
        sample = samples[0]
        assert sample.prompt == "a glass office building in a city plaza"
        assert sample.step == 250
        assert sample.seed == 3001
        assert sample.generation_metadata["sample_track"] == "building"
        assert sample.generation_metadata["strength"] == 1.0


def test_unified_training_reference_reuses_populated_canonical_dataset(client: TestClient):
    project = client.post("/api/projects", json={"title": "dawnjian"}).json()
    with app_module.SessionLocal() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project["id"],
            kind=models.AssetKind.image,
            name="source.png",
            mime_type="image/png",
        )
        canonical = models.Dataset(project_id=project["id"], name="dawnjian")
        unified = models.Dataset(project_id=project["id"], name="dawnjian_unified")
        db.add_all([asset, canonical, unified])
        db.flush()
        canonical_version = models.DatasetVersion(
            dataset_id=canonical.id,
            version_number=1,
            name="dawnjian",
            source_uri="s3://titlesxyz/title-lora/datasets/dawnjian",
            caption_format="text",
            status="published",
        )
        unified_version = models.DatasetVersion(
            dataset_id=unified.id,
            version_number=1,
            name="dawnjian_unified",
            source_uri="training-path:///workspace/aitk/datasets/dawnjian_unified",
            caption_format="text",
            status="referenced",
        )
        db.add_all([canonical_version, unified_version])
        db.flush()
        db.add(
            models.DatasetItem(
                dataset_version_id=canonical_version.id,
                asset_id=asset.id,
                caption="source image",
                caption_format="text",
                included=True,
                position=0,
            )
        )
        run = models.TrainingRun(
            project_id=project["id"],
            dataset_version_id=unified_version.id,
            name="dawnjian-krea2-unified-overfit",
            status="completed",
            normalized_config={"dataset_sources": ["/workspace/aitk/datasets/dawnjian_unified"]},
        )
        db.add(run)
        db.flush()

        SQLAlchemyImportSink(db)._link_dataset(run)

        assert run.dataset_version_id == canonical_version.id
