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
        assert definition.prompt_set_id is None
        assert [item["text"] for item in definition.inline_prompts] == [prompt]
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
        assert session.scalar(select(func.count()).select_from(models.PromptSet)) == 0
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
    assert "incompatible with base model" in mismatch.json()["detail"]

    with app_module.SessionLocal.begin() as session:
        stored = session.get(models.ModelVersion, version["id"])
        stored.readiness = {}
    unavailable = client.post("/api/generation-requests/compile", json=_payload(project["id"], model, version, str(uuid4()), "unavailable"))
    assert unavailable.status_code == 409
    assert "not registered with FAL" in unavailable.json()["detail"]


def test_eval_compile_binds_exact_saved_prompt_set_version(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Saved prompt Eval"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    created = client.post(
        "/api/prompt-sets",
        headers=headers,
        json={"project_id": project["id"], "name": "Angles", "prompts": ["front portrait", "profile portrait"]},
    ).json()
    prompt_set = client.get(f"/api/prompt-sets/{created['id']}").json()
    prompts = [{"prompt_id": prompt["id"], "prompt": prompt["text"]} for prompt in prompt_set["prompts"]]
    payload = {
        "workflow": "eval",
        "provider": "fal",
        "context": {"kind": "project", "project_id": project["id"]},
        "model_version_id": version["id"],
        "checkpoint_revision_id": version["checkpoint_revision_id"],
        "prompt_set_id": created["id"],
        "prompts": prompts,
        "parameters": {"lora_scale": 1, "num_images": 1},
        "client_request_id": str(uuid4()),
    }

    compiled = client.post("/api/eval-generation-requests/compile", json=payload)
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["prompt_set_id"] == created["id"]
    with app_module.SessionLocal() as session:
        definition = session.get(models.EvalDefinition, compiled.json()["definition_id"])
        assert definition.prompt_set_id == created["id"]
        assert [item["id"] for item in definition.inline_prompts] == [prompt["prompt_id"] for prompt in prompts]

    changed = {**payload, "client_request_id": str(uuid4()), "prompts": [{**prompts[0], "prompt": "edited"}, prompts[1]]}
    mismatch = client.post("/api/eval-generation-requests/compile", json=changed)
    assert mismatch.status_code == 409
    assert "do not match" in mismatch.json()["detail"]


def test_grid_preflight_binds_prompt_set_version_and_rejects_unsupported_krea_axes(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Schema Grid"}).json()
    _model, version = _registered_checkpoint(client, project["id"])
    prompt_set = client.post(
        "/api/prompt-sets",
        headers=headers,
        json={"project_id": project["id"], "name": "Grid prompts", "prompts": ["front portrait", "profile portrait"]},
    ).json()
    payload = {
        "model_version_id": version["id"],
        "prompt_set_id": prompt_set["id"],
        "endpoint": "fal-ai/krea-2/turbo/lora",
        "x_axis": {"field": "prompt", "values": ["front portrait", "profile portrait"]},
        "y_axis": {"field": "seed", "values": [1, 2]},
        "parameters": {},
    }

    created = client.post("/api/grids", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["prompt_set_id"] == prompt_set["id"]

    unsupported = client.post(
        "/api/grids",
        headers=headers,
        json={**payload, "x_axis": {"field": "guidance_scale", "values": [3, 5]}},
    )
    assert unsupported.status_code == 422
    assert "supported by the endpoint" in unsupported.json()["detail"]


def test_worker_plan_keeps_legacy_prompt_set_compatibility(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Legacy"}).json()
    _model, version = _registered_checkpoint(client, project["id"])
    with app_module.SessionLocal.begin() as session:
        prompt_set = models.PromptSet(project_id=project["id"], name="Legacy prompts", version=1)
        session.add(prompt_set); session.flush()
        prompt = models.Prompt(prompt_set_id=prompt_set.id, text="legacy prompt", position=0, metadata_={})
        session.add(prompt)
        definition = models.EvalDefinition(project_id=project["id"], name="Legacy", endpoint="fal-ai/krea-2/turbo/lora", model_version_id=version["id"], prompt_set_id=prompt_set.id, inline_prompts=[], parameters={})
        session.add(definition); session.flush()
        run = models.EvalRun(definition_id=definition.id, checkpoint_id=version["checkpoint_id"], status="test-plan", parameters_snapshot={})
        session.add(run); session.flush(); run_id = run.id
    handler = object.__new__(FalEvalBatchHandler); handler.session_factory = app_module.SessionLocal
    _definition, _checkpoint, prompts = handler._plan(run_id)
    assert [item.text for item in prompts] == ["legacy prompt"]
    assert prompts[0].id == prompt.id


def test_inline_output_lookup_is_independent_for_each_logical_prompt(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Two inline prompts"}).json()
    _model, version = _registered_checkpoint(client, project["id"])
    inline = [
        {"id": "inline-first", "text": "first prompt", "position": 0, "metadata": {}},
        {"id": "inline-second", "text": "second prompt", "position": 1, "metadata": {}},
    ]
    created = client.post("/api/eval-definitions", json={"project_id": project["id"], "name": "Inline pair", "endpoint": "fal-ai/krea-2/turbo/lora", "model_version_id": version["id"], "inline_prompts": inline, "parameters": {}})
    assert created.status_code == 201, created.text
    assert created.json()["inline_prompts"] == inline
    with app_module.SessionLocal.begin() as session:
        workspace_id = session.get(models.Project, project["id"]).workspace_id
        definition = session.get(models.EvalDefinition, created.json()["id"])
        run = models.EvalRun(definition_id=definition.id, checkpoint_id=version["checkpoint_id"], status="running", parameters_snapshot={})
        asset = models.Asset(workspace_id=workspace_id, project_id=project["id"], kind=models.AssetKind.image, name="first.png")
        session.add_all([run, asset]); session.flush()
        session.add(models.EvalOutput(eval_run_id=run.id, prompt_id=None, asset_id=asset.id, provider_metadata={"inline_prompt_id": "inline-first"}))
        run_id = run.id

    handler = object.__new__(FalEvalBatchHandler); handler.session_factory = app_module.SessionLocal
    _definition, _checkpoint, planned = handler._plan(run_id)
    assert [item.id for item in planned] == ["inline-first", "inline-second"]
    assert handler._has_output(run_id, None, inline_prompt_id="inline-first") is True
    assert handler._has_output(run_id, None, inline_prompt_id="inline-second") is False


def test_generation_prompt_affixes_apply_to_image_and_eval_compile(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Affixes"}).json()
    model, version = _registered_checkpoint(client, project["id"])

    settings = client.patch(
        "/api/operator-settings",
        json={"generation": {"prompt_prepend": "house style", "prompt_append": "no watermark"}},
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["generation"] == {"prompt_prepend": "house style", "prompt_append": "no watermark"}

    image = client.post(
        "/api/generation-requests/compile",
        json=_payload(project["id"], model, version, str(uuid4()), "portrait"),
    )
    assert image.status_code == 200, image.text

    eval_compile = client.post(
        "/api/eval-generation-requests/compile",
        json={
            "workflow": "eval",
            "provider": "fal",
            "context": {"kind": "project", "project_id": project["id"]},
            "model_version_id": version["id"],
            "checkpoint_revision_id": version["checkpoint_revision_id"],
            "prompts": [{"prompt_id": "prompt-1", "prompt": "profile"}],
            "parameters": {"lora_scale": 1, "num_images": 1},
            "client_request_id": str(uuid4()),
        },
    )
    assert eval_compile.status_code == 200, eval_compile.text

    with app_module.SessionLocal() as session:
        image_definition = session.get(models.EvalDefinition, image.json()["definition_id"])
        eval_definition = session.get(models.EvalDefinition, eval_compile.json()["definition_id"])
        assert [item["text"] for item in image_definition.inline_prompts] == ["house style portrait no watermark"]
        assert [item["text"] for item in eval_definition.inline_prompts] == ["house style profile no watermark"]
