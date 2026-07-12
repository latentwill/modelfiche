from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from titles_api import app as app_module, models
from titles_worker.fal_jobs import FalEvalBatchHandler


def _registered_checkpoint(client: TestClient, project_id: str):
    profile = client.get("/api/profiles").json()[0]
    run = client.post("/api/runs", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project_id, "name": "generation run"}).json()
    asset = client.post("/api/assets", json={"project_id": project_id, "kind": "model", "name": "model.safetensors"}).json()
    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project_id).workspace_id
        session.add(models.AssetLocation(asset_id=asset["id"], workspace_id=workspace_id, provider="s3", uri="s3://bucket/model.safetensors", bucket="bucket", object_key="model.safetensors", version_id="v1", verification_state="available", hydration_state="remote"))
        if session.scalar(select(models.LocalRoot).where(models.LocalRoot.workspace_id == workspace_id, models.LocalRoot.kind == "spool")) is None:
            session.add(models.LocalRoot(workspace_id=workspace_id, kind="spool", canonical_path="/tmp/modelfiche-test-spool", fingerprint={"test": True}, owner_uid=501, mode=0o700, availability="available"))
    checkpoint = client.post(f"/api/runs/{run['id']}/checkpoints", headers={"X-Profile-ID": profile["id"]}, json={"asset_id": asset["id"], "step": 100, "state": "available"}).json()
    model = client.post("/api/models", headers={"X-Profile-ID": profile["id"]}, json={"project_id": project_id, "name": "generation model"}).json()
    version = client.post("/api/model-versions", headers={"X-Profile-ID": profile["id"]}, json={"model_id": model["id"], "checkpoint_id": checkpoint["id"], "name": "v1"}).json()
    with app_module.SessionLocal.begin() as session:
        stored = session.get(models.ModelVersion, version["id"])
        stored.readiness = {"fal_url": "https://example.test/model.safetensors", "endpoint_id": "fal-ai/krea-2/turbo/lora"}
    return model, client.get(f"/api/model-versions/{version['id']}").json()


def _payload(project_id: str, model: dict, version: dict, client_request_id: str, prompt: str):
    return {
        "workflow": "image", "provider": "fal",
        "context": {"kind": "model", "project_id": project_id, "model_id": model["id"]},
        "model_version_id": version["id"], "checkpoint_revision_id": version["checkpoint_revision_id"],
        "prompt": prompt,
        "parameters": {"endpoint_id": "fal-ai/krea-2/turbo/lora", "lora_scale": 1, "num_images": 1},
        "client_request_id": client_request_id,
    }


def test_image_compile_is_exact_admissible_and_idempotent(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Compile"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    request_id = str(uuid4())
    prompt = "exact image prompt, not an older eval prompt"

    first = client.post("/api/generation-requests/compile", json=_payload(project["id"], model, version, request_id, prompt))
    assert first.status_code == 200, first.text
    compiled = first.json()
    assert compiled["admission"]["state"] == "ready"
    assert compiled["admission"]["frozen"]["model_version_id"] == version["id"]

    with app_module.SessionLocal.begin() as session:
        definition = session.get(models.EvalDefinition, compiled["definition_id"])
        prompts = list(session.scalars(select(models.Prompt).where(models.Prompt.prompt_set_id == definition.prompt_set_id).order_by(models.Prompt.position)))
        assert [item.text for item in prompts] == [prompt]
        run = models.EvalRun(definition_id=definition.id, checkpoint_id=version["checkpoint_id"], checkpoint_revision_id=version["checkpoint_revision_id"], status="test-plan", parameters_snapshot=definition.parameters)
        session.add(run)
        session.flush()
        run_id = run.id

    # Exercise the worker's real plan loader without admitting or contacting the provider.
    handler = object.__new__(FalEvalBatchHandler)
    handler.session_factory = app_module.SessionLocal
    planned_definition, planned_checkpoint, planned_prompts = handler._plan(run_id)
    assert planned_definition.id == compiled["definition_id"]
    assert planned_checkpoint == version["checkpoint_id"]
    assert [item.text for item in planned_prompts] == [prompt]

    second = client.post("/api/generation-requests/compile", json=_payload(project["id"], model, version, request_id, prompt))
    assert second.status_code == 200
    assert second.json()["admission"]["id"] == compiled["admission"]["id"]
    assert second.json()["definition_id"] == compiled["definition_id"]
    with app_module.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(models.PromptSet)) == 1
        assert session.scalar(select(func.count()).select_from(models.EvalDefinition)) == 1

    conflict = _payload(project["id"], model, version, request_id, "different prompt")
    response = client.post("/api/generation-requests/compile", json=conflict)
    assert response.status_code == 409
    assert "different generation request" in response.json()["detail"]


def test_image_compile_rejects_cross_project_without_side_effects(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Owner"}).json()
    other = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Other"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    payload = _payload(other["id"], model, version, str(uuid4()), "must not persist")
    payload["context"]["model_id"] = model["id"]

    response = client.post("/api/generation-requests/compile", json=payload)
    assert response.status_code == 409
    assert "another project" in response.json()["detail"]
    with app_module.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(models.PromptSet)) == 0
        assert session.scalar(select(func.count()).select_from(models.EvalDefinition)) == 0


def test_image_compile_requires_matching_registered_endpoint(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Registration"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    payload = _payload(project["id"], model, version, str(uuid4()), "registered")
    payload["parameters"]["endpoint_id"] = "ideogram/v4/lora"
    mismatch = client.post("/api/generation-requests/compile", json=payload)
    assert mismatch.status_code == 409
    assert "does not match" in mismatch.json()["detail"]

    with app_module.SessionLocal.begin() as session:
        stored = session.get(models.ModelVersion, version["id"])
        stored.readiness = {}
    unavailable = client.post("/api/generation-requests/compile", json=_payload(project["id"], model, version, str(uuid4()), "unavailable"))
    assert unavailable.status_code == 409
    assert "not registered with FAL" in unavailable.json()["detail"]
