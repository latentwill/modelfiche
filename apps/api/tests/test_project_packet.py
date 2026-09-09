from datetime import datetime, timezone

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


def test_fal_admission_activity_resolves_eval_run_and_latest_image(client: TestClient):
    profile, project = _profile_and_project(client)
    older_image = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "older.png", "mime_type": "image/png"},
    ).json()
    latest_image = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "latest.png", "mime_type": "image/png"},
    ).json()
    model_asset = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "model", "name": "weights.safetensors"},
    ).json()

    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        prompt_set = models.PromptSet(project_id=project["id"], name="Admission prompts")
        session.add(prompt_set)
        session.flush()
        definition = models.EvalDefinition(
            project_id=project["id"],
            name="FAL quality",
            endpoint="fal-ai/test",
            prompt_set_id=prompt_set.id,
        )
        session.add(definition)
        session.flush()
        run = models.EvalRun(definition_id=definition.id, status="running")
        session.add(run)
        session.flush()
        session.add_all(
            [
                models.EvalOutput(
                    eval_run_id=run.id,
                    asset_id=older_image["id"],
                    generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                models.EvalOutput(
                    eval_run_id=run.id,
                    asset_id=model_asset["id"],
                    generated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                ),
                models.EvalOutput(
                    eval_run_id=run.id,
                    asset_id=latest_image["id"],
                    generated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ),
            ]
        )
        session.add(
            models.ActivityEvent(
                workspace_id=workspace_id,
                project_id=project["id"],
                profile_id=profile["id"],
                action="fal.admission_admitted",
                subject_type="fal_admission",
                subject_id="admission-eval",
                details={"eval_run_id": run.id, "job_id": "missing-job"},
            )
        )

    event = client.get("/api/activity", params={"project_id": project["id"]}).json()[0]
    assert event["object_href"] == f"#/eval/{run.id}"
    assert event["object_label"] == "FAL quality"
    assert event["preview_asset_id"] == latest_image["id"]
    assert event["summary"] == "Generation admitted"


def test_generation_activity_consolidates_admission_and_hides_filename(client: TestClient):
    profile, project = _profile_and_project(client)
    image = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "generated-output.png", "mime_type": "image/png"},
    ).json()
    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        prompt_set = models.PromptSet(project_id=project["id"], name="Generation prompts")
        session.add(prompt_set)
        session.flush()
        definition = models.EvalDefinition(
            project_id=project["id"],
            name="Generation run",
            endpoint="fal-ai/test",
            prompt_set_id=prompt_set.id,
        )
        session.add(definition)
        session.flush()
        run = models.EvalRun(definition_id=definition.id, status="running")
        session.add(run)
        session.flush()
        session.add_all(
            [
                models.ActivityEvent(
                    workspace_id=workspace_id,
                    project_id=project["id"],
                    profile_id=profile["id"],
                    action="fal.admission_admitted",
                    subject_type="fal_admission",
                    subject_id="admission-generation",
                    details={"eval_run_id": run.id, "job_id": "job-generation"},
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                models.ActivityEvent(
                    workspace_id=workspace_id,
                    project_id=project["id"],
                    profile_id=None,
                    action="asset.generated",
                    subject_type="asset",
                    subject_id=image["id"],
                    details={
                        "summary": "Generated generated-output.png",
                        "provider": "fal",
                        "endpoint": "fal-ai/test",
                        "eval_run_id": run.id,
                    },
                    created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ),
            ]
        )

    events = client.get("/api/activity", params={"project_id": project["id"]}).json()
    generated = [event for event in events if event["action"] == "image.generated"]
    assert len(generated) == 1
    event = generated[0]
    assert event["event_type"] == "image.generated"
    assert event["summary"] == "Image generated"
    assert event["details"] == {
        "eval_run_id": run.id,
        "job_id": "job-generation",
        "provider": "fal",
        "endpoint": "fal-ai/test",
    }
    assert event["profile_id"] == profile["id"]
    assert event["object_href"] == f"#/eval/{run.id}"
    assert event["object_label"] == "Generation run"
    assert event["preview_asset_id"] == image["id"]
    assert "generated-output.png" not in str(event)
    assert not any(event["action"] == "fal.admission_admitted" for event in events)

    image_events = client.get(
        "/api/activity",
        params={"project_id": project["id"], "subject_type": "asset", "subject_id": image["id"]},
    ).json()
    assert [event["action"] for event in image_events].count("image.generated") == 1
    assert not any(event["action"] == "fal.admission_admitted" for event in image_events)


def test_fal_admission_activity_falls_back_to_job(client: TestClient):
    profile, project = _profile_and_project(client)
    image = client.post(
        "/api/assets",
        json={"project_id": project["id"], "kind": "image", "name": "job-preview.png", "mime_type": "image/png"},
    ).json()
    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        job = models.Job(
            workspace_id=workspace_id,
            profile_id=profile["id"],
            kind="fal.eval_admission",
            result={"asset_ids": [image["id"]]},
        )
        session.add(job)
        session.flush()
        session.add(
            models.ActivityEvent(
                workspace_id=workspace_id,
                project_id=project["id"],
                profile_id=profile["id"],
                action="fal.admission_admitted",
                subject_type="fal_admission",
                subject_id="admission-job",
                details={"eval_run_id": "missing-eval", "job_id": job.id},
            )
        )

    event = client.get("/api/activity", params={"project_id": project["id"]}).json()[0]
    assert event["object_href"] == f"#/jobs/{job.id}?job=1"
    assert event["object_label"] == "Transfer job · fal.eval_admission"
    assert event["preview_asset_id"] == image["id"]


def test_fal_admission_activity_leaves_stale_targets_unenriched(client: TestClient):
    profile, project = _profile_and_project(client)
    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        session.add(
            models.ActivityEvent(
                workspace_id=workspace_id,
                project_id=project["id"],
                profile_id=profile["id"],
                action="fal.admission_admitted",
                subject_type="fal_admission",
                subject_id="admission-stale",
                details={"eval_run_id": "missing-eval", "job_id": "missing-job"},
            )
        )

    event = client.get("/api/activity", params={"project_id": project["id"]}).json()[0]
    assert event["object_href"] is None
    assert event["object_label"] is None
    assert event["preview_asset_id"] is None


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
