from fastapi.testclient import TestClient

from titles_api import app as app_module, models


def _operator_project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Model review"}).json()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"project_id": project["id"], "name": "Portrait inputs", "items": []},
    ).json()
    dataset_detail = client.get(f"/api/datasets/{dataset['id']}").json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "dataset_version_id": dataset_detail["current_version_id"],
            "name": "Portrait training",
        },
    ).json()
    return profile, headers, project, dataset, dataset_detail, run


def _checkpoint(client: TestClient, headers: dict[str, str], project_id: str, run_id: str, step: int):
    asset = client.post(
        "/api/assets",
        headers=headers,
        json={
            "project_id": project_id,
            "kind": "model",
            "name": f"portrait_{step:09d}.safetensors",
            "location": {
                "provider": "s3",
                "uri": f"s3://models/runs/portrait_{step:09d}.safetensors",
                "bucket": "models",
                "object_key": f"runs/portrait_{step:09d}.safetensors",
                "size": step * 1000,
            },
        },
    ).json()
    checkpoint = client.post(
        f"/api/runs/{run_id}/checkpoints",
        headers=headers,
        json={"asset_id": asset["id"], "step": step, "state": "available"},
    ).json()
    return asset, checkpoint


def test_samples_project_true_checkpoint_steps_stable_prompt_identity_and_attribution(client: TestClient):
    profile, headers, project, _dataset, _dataset_detail, run = _operator_project(client)
    _, checkpoint_250 = _checkpoint(client, headers, project["id"], run["id"], 250)
    _, checkpoint_500 = _checkpoint(client, headers, project["id"], run["id"], 500)

    sample_ids = []
    for step, checkpoint in ((250, checkpoint_250), (500, checkpoint_500)):
        asset = client.post(
            "/api/assets",
            headers=headers,
            json={"project_id": project["id"], "kind": "image", "name": f"1783088373171__{step:09d}_0.jpg"},
        ).json()
        created = client.post(
            f"/api/runs/{run['id']}/samples",
            json={"asset_id": asset["id"], "checkpoint_id": checkpoint["id"], "step": 0, "prompt": "A studio portrait", "seed": 42},
        )
        assert created.status_code == 201
        sample_ids.append(asset["id"])

    review = client.post(
        "/api/reviews",
        headers=headers,
        json={"subject_type": "asset", "subject_id": sample_ids[1], "rating": 4, "decision": "approved"},
    )
    assert review.status_code == 201

    samples = client.get(f"/api/runs/{run['id']}/samples", params={"limit": 1000}).json()
    assert {sample["training_step"] for sample in samples} == {250, 500}
    assert {sample["sample_identity"] for sample in samples} == {"sample-0"}
    assert {sample["sample_label"] for sample in samples} == {"A studio portrait"}
    assert {sample["checkpoint_id"] for sample in samples} == {checkpoint_250["id"], checkpoint_500["id"]}
    reviewed = next(sample for sample in samples if sample["asset_id"] == sample_ids[1])
    assert reviewed["decision"] == "approved"
    assert reviewed["rating"] == 4
    assert reviewed["reviewed_by"] == profile["display_name"]
    assert reviewed["reviewed_at"]


def test_checkpoint_version_readiness_dataset_and_typed_lineage_are_consistent(client: TestClient):
    _profile, headers, project, dataset, dataset_detail, run = _operator_project(client)
    asset, checkpoint = _checkpoint(client, headers, project["id"], run["id"], 250)
    model = client.post("/api/models", headers=headers, json={"project_id": project["id"], "name": "Portrait LoRA"}).json()
    version = client.post(
        "/api/model-versions",
        headers=headers,
        json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "Portrait step 250"},
    ).json()

    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        session.add(
            models.AssetLocation(
                asset_id=asset["id"],
                workspace_id=workspace_id,
                provider="local",
                uri="file:///managed/portrait_250.safetensors",
                relative_path="managed/portrait_250.safetensors",
                size=250_000,
                verification_state="verified",
                hydration_state="hydrated",
            )
        )
        session.get(models.Checkpoint, checkpoint["id"]).state = "hydrated"

    checkpoint_detail = client.get(f"/api/checkpoints/{checkpoint['id']}").json()
    assert checkpoint_detail["registered_version"]["id"] == version["id"]
    assert checkpoint_detail["readiness_summary"]["local"] == {
        "status": "hydrated",
        "available": True,
        "can_hydrate": False,
        "reason": "A verified local copy is already available.",
        "path": "managed/portrait_250.safetensors",
        "verification_state": "verified",
        "job_id": None,
    }
    dataset_version_name = client.get(f"/api/dataset-versions/{dataset_detail['current_version_id']}").json()["name"]
    assert checkpoint_detail["dataset"] == {
        "dataset_id": dataset["id"],
        "dataset_name": "Portrait inputs",
        "dataset_version_id": dataset_detail["current_version_id"],
        "dataset_version_name": dataset_version_name,
        "available": True,
        "input_count": 1,
    }
    impossible_hydration = client.post(f"/api/checkpoints/{checkpoint['id']}/hydrate", headers=headers)
    assert impossible_hydration.status_code == 409
    assert "already available" in impossible_hydration.json()["detail"]

    duplicate = client.post(
        "/api/model-versions",
        headers=headers,
        json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "Duplicate"},
    )
    assert duplicate.status_code == 409
    assert "already registered" in duplicate.json()["detail"]

    listed_run = client.get("/api/runs", params={"project_id": project["id"]}).json()[0]
    detailed_run = client.get(f"/api/runs/{run['id']}").json()
    version_detail = client.get(f"/api/model-versions/{version['id']}").json()
    assert listed_run["dataset_id"] == detailed_run["dataset_id"] == dataset["id"]
    assert listed_run["dataset_version_name"] == detailed_run["dataset_version_name"] == dataset_version_name
    assert version_detail["dataset"]["dataset_id"] == dataset["id"]
    assert version_detail["readiness_summary"]["local"]["status"] == "hydrated"

    lineage = client.get(
        "/api/lineage",
        params={"subject_type": "training_run", "subject_id": run["id"], "depth": 3},
    ).json()
    relationships = {(edge["source_type"], edge["relationship"], edge["target_type"]) for edge in lineage}
    assert ("dataset_version", "trained_with", "training_run") in relationships
    assert ("training_run", "produced", "checkpoint") in relationships
    assert ("checkpoint", "registered_as", "model_version") in relationships
    registered_edge = next(edge for edge in lineage if edge["relationship"] == "registered_as")
    assert registered_edge["source_href"] == f"#/checkpoint/{checkpoint['id']}"
    assert registered_edge["target_href"] == f"#/model-version/{version['id']}"
