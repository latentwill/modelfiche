from uuid import uuid4

from sqlalchemy import func, select

from titles_api import app as app_module, models
from titles_api.routers.generation_queue import claim_next_for_worker, queue_capability
from titles_worker.runner import JobRunner
from test_generation_compile import _payload, _registered_checkpoint


def _compile_and_queue(client, project, model, version, prompt="queued image"):
    request_id = str(uuid4())
    normalized = _payload(project["id"], model, version, request_id, prompt)
    compiled = client.post("/api/generation-requests/compile", json=normalized).json()
    body = {"workflow": "image", "provider": "fal", "context": normalized["context"],
        "model_version_ids": [version["id"]], "compiled_request_id": request_id,
        "admission_id": compiled["admission"]["id"], "client_request_id": request_id, "request": normalized}
    return body, client.post("/api/generation-queue", json=body)


def test_queue_is_durable_fifo_and_idempotent_without_fal_dispatch(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Queue"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    first_body, first = _compile_and_queue(client, project, model, version, "first")
    _, second = _compile_and_queue(client, project, model, version, "second")
    assert first.status_code == second.status_code == 202
    replay = client.post("/api/generation-queue", json=first_body)
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    items = client.get("/api/generation-queue").json()["items"]
    assert [item["id"] for item in items] == [first.json()["id"], second.json()["id"]]
    assert all(item["status"] == "queued" and item["queue_owner"] == "dam" for item in items)
    # Compilation + enqueue never admits a billable provider job.
    with app_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(models.Job)) == 0
        assert db.get(models.FalAdmission, first.json()["admission_id"]).state == "ready"


def test_claim_and_status_normalization_persist(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Lifecycle"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    _, queued = _compile_and_queue(client, project, model, version)
    claimed = client.post("/api/generation-queue/claim").json()
    assert claimed["id"] == queued.json()["id"] and claimed["status"] == "running"
    assert claimed["job_id"] and claimed["provider_job_id"]
    # A second claim cannot admit the same admission twice.
    assert client.post("/api/generation-queue/claim").json() is None
    with app_module.SessionLocal() as db:
        admission = db.get(models.FalAdmission, claimed["admission_id"])
        assert admission.state == "admitted"
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.payload["admission_id"].as_string() == admission.id)) == 1
    completed = client.patch(f"/api/generation-queue/{claimed['id']}", json={"status": "succeeded", "progress": {"completed": 1, "total": 1}, "result": {"asset_ids": ["asset-1"]}, "provider_job_id": "fal-1"})
    assert completed.json()["status"] == "completed"
    reloaded = client.get(f"/api/generation-queue/{claimed['id']}").json()
    assert reloaded["result"]["asset_ids"] == ["asset-1"] and reloaded["provider_job_id"] == "fal-1"


def test_worker_claims_the_oldest_job_across_workspaces(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Secondary workspace queue"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    _, queued = _compile_and_queue(client, project, model, version)
    with app_module.SessionLocal.begin() as db:
        workspace = models.Workspace(name="Secondary")
        db.add(workspace)
        db.flush()
        db.get(models.Project, project["id"]).workspace_id = workspace.id
        item = db.get(models.GenerationQueueItem, queued.json()["id"])
        item.workspace_id = workspace.id
        db.get(models.FalAdmission, item.admission_id).workspace_id = workspace.id

    with app_module.SessionLocal() as db:
        claimed = claim_next_for_worker(db)

    assert claimed["id"] == queued.json()["id"]
    assert claimed["status"] == "running"
    assert claimed["job_id"]


def test_worker_terminal_sync_and_restart_recovery(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Worker sync"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    _, queued = _compile_and_queue(client, project, model, version)
    claimed = client.post("/api/generation-queue/claim").json()
    with app_module.SessionLocal.begin() as db:
        job = db.get(models.Job, claimed["job_id"]); job.state = models.JobState.running
    runner = JobRunner(app_module.SessionLocal, {"fal.eval_admission": lambda context, payload: {"asset_ids": ["done"]}})
    assert runner.recover_interrupted() == 1
    recovered = client.get(f"/api/generation-queue/{queued.json()['id']}").json()
    assert recovered["status"] == "queued" and "resuming" in recovered["error"]
    assert runner.run_one()
    terminal = client.get(f"/api/generation-queue/{queued.json()['id']}").json()
    assert terminal["status"] == "completed" and terminal["result"] == {"asset_ids": ["done"]}


def test_failed_generation_can_be_retried_from_persisted_job_payload(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Retry"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    _, queued = _compile_and_queue(client, project, model, version)
    claimed = client.post("/api/generation-queue/claim").json()
    failing = JobRunner(app_module.SessionLocal, {"fal.eval_admission": lambda context, payload: (_ for _ in ()).throw(RuntimeError("provider timeout"))})

    assert failing.run_one()
    failed = client.get(f"/api/generation-queue/{queued.json()['id']}").json()
    assert failed["status"] == "failed"
    retried = client.post(f"/api/generation-queue/{failed['id']}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["job_id"] != claimed["job_id"]

    succeeding = JobRunner(app_module.SessionLocal, {"fal.eval_admission": lambda context, payload: {"asset_ids": ["recovered"]}})
    assert succeeding.run_one()
    recovered = client.get(f"/api/generation-queue/{failed['id']}").json()
    assert recovered["status"] == "completed"
    assert recovered["result"] == {"asset_ids": ["recovered"]}


def test_provider_managed_queue_is_never_scheduled_internally(client):
    assert queue_capability("comfyui") == {"owner": "provider", "dispatch": "provider_native"}
    body = {"workflow": "image", "provider": "comfyui", "context": {"project_id": str(uuid4())},
        "model_version_ids": [str(uuid4())], "compiled_request_id": str(uuid4()), "client_request_id": str(uuid4()), "request": {}}
    response = client.post("/api/generation-queue", json=body)
    assert response.status_code == 422
    assert "provider-native queue" in response.json()["detail"]


def test_enqueue_rejects_model_or_compiled_binding_tampering(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Binding"}).json()
    model, version = _registered_checkpoint(client, project["id"])
    body, _queued = _compile_and_queue(client, project, model, version)
    body["client_request_id"] = str(uuid4())
    body["compiled_request_id"] = body["client_request_id"]
    body["model_version_ids"] = [str(uuid4())]
    response = client.post("/api/generation-queue", json=body)
    assert response.status_code == 404

    body["model_version_ids"] = [version["id"]]
    response = client.post("/api/generation-queue", json=body)
    assert response.status_code == 409
    assert "compiled FAL admission" in response.json()["detail"]


def test_multi_model_eval_is_one_ordered_parent_with_exactly_once_children_and_results(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Comparison"}).json()
    model_a, version_a = _registered_checkpoint(client, project["id"])
    model_b, version_b = _registered_checkpoint(client, project["id"])
    prompts = [{"prompt_id": "prompt-a", "prompt": "first prompt"}, {"prompt_id": "prompt-b", "prompt": "second prompt"}]
    children = []
    for model, version in ((model_a, version_a), (model_b, version_b)):
        request_id = str(uuid4())
        compiled = client.post("/api/eval-generation-requests/compile", json={
            "workflow": "eval", "provider": "fal", "context": {"kind": "project", "project_id": project["id"]},
            "model_version_id": version["id"], "checkpoint_revision_id": version["checkpoint_revision_id"],
            "prompts": prompts, "parameters": {"endpoint_id": "fal-ai/krea-2/turbo/lora", "lora_scale": 1, "num_images": 1},
            "client_request_id": request_id,
        })
        assert compiled.status_code == 200, compiled.text
        children.append({"model_version_id": version["id"], "compiled_request_id": request_id, "admission_id": compiled.json()["admission"]["id"]})
    parent_id = str(uuid4())
    queued = client.post("/api/generation-queue", json={"workflow": "eval", "provider": "fal",
        "context": {"kind": "project", "project_id": project["id"]},
        "model_version_ids": [version_a["id"], version_b["id"]], "compiled_request_id": parent_id,
        "client_request_id": parent_id, "request": {"prompts": prompts}, "children": children})
    assert queued.status_code == 202, queued.text
    assert [child["model_version_id"] for child in queued.json()["children"]] == [version_a["id"], version_b["id"]]
    first = client.post("/api/generation-queue/claim").json()
    assert first["children"][0]["status"] == "running" and first["children"][1]["status"] == "queued"
    with app_module.SessionLocal.begin() as db:
        job = db.get(models.Job, first["job_id"]); job.state = models.JobState.running
    runner = JobRunner(app_module.SessionLocal, {"fal.eval_admission": lambda context, payload: {"asset_ids": []}})
    assert runner.recover_interrupted() == 1
    assert runner.run_one() and runner.run_one()
    terminal = client.get(f"/api/generation-queue/{queued.json()['id']}").json()
    assert terminal["status"] == "completed"
    assert [child["status"] for child in terminal["children"]] == ["completed", "completed"]
    with app_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.kind == "fal.eval_admission")) == 2
    with app_module.SessionLocal.begin() as db:
        workspace_id = db.get(models.Project, project["id"]).workspace_id
        queue_children = list(db.scalars(select(models.GenerationQueueChild).where(
            models.GenerationQueueChild.queue_item_id == queued.json()["id"]).order_by(models.GenerationQueueChild.ordinal)))
        for child_index, child in enumerate(queue_children):
            admission = db.get(models.FalAdmission, child.admission_id)
            for prompt_index, prompt in enumerate(prompts):
                asset = models.Asset(workspace_id=workspace_id, project_id=project["id"], kind="image", name=f"child-{child_index}-{prompt_index}.jpg",
                    mime_type="image/jpeg", metadata_={"category": "eval_output"})
                db.add(asset); db.flush()
                db.add(models.EvalOutput(eval_run_id=admission.eval_run_id, asset_id=asset.id,
                    seed=child_index * 10 + prompt_index,
                    provider_metadata={"inline_prompt_id": prompt["prompt_id"]}))
        unrelated_definition = models.EvalDefinition(project_id=project["id"], name="Unrelated", endpoint="fal-ai/krea-2/turbo/lora",
            model_version_id=version_a["id"], prompt_set_id=None,
            inline_prompts=[{"id": "prompt-a", "text": "first prompt", "position": 0}], parameters={})
        db.add(unrelated_definition); db.flush()
        unrelated_run = models.EvalRun(definition_id=unrelated_definition.id, status="succeeded")
        db.add(unrelated_run); db.flush()
        unrelated_asset = models.Asset(workspace_id=workspace_id, project_id=project["id"], kind="image", name="unrelated.jpg",
            mime_type="image/jpeg", metadata_={"category": "eval_output"})
        db.add(unrelated_asset); db.flush()
        db.add(models.EvalOutput(eval_run_id=unrelated_run.id, asset_id=unrelated_asset.id,
            provider_metadata={"inline_prompt_id": "prompt-a"}))
        unrelated_asset_id = unrelated_asset.id
    comparison = client.get(f"/api/generation-queue/{queued.json()['id']}/eval-results").json()
    assert [run["model_version_id"] for run in comparison["runs"]] == [version_a["id"], version_b["id"]]
    assert [[prompt["id"] for prompt in run["prompts"]] for run in comparison["runs"]] == [["prompt-a", "prompt-b"], ["prompt-a", "prompt-b"]]
    assert [[output["prompt_id"] for output in run["outputs"]] for run in comparison["runs"]] == [["prompt-a", "prompt-b"], ["prompt-a", "prompt-b"]]
    assert [[output["seed"] for output in run["outputs"]] for run in comparison["runs"]] == [[0, 1], [10, 11]]
    assert unrelated_asset_id not in {output["asset_id"] for run in comparison["runs"] for output in run["outputs"]}


def test_grid_parent_accepts_repeated_cell_models_and_unique_z_models(client):
    profile = client.get("/api/profiles").json()[0]
    project = client.post("/api/projects", headers={"X-Profile-ID": profile["id"]}, json={"title": "Grid queue"}).json()
    model_a, version_a = _registered_checkpoint(client, project["id"])
    model_b, version_b = _registered_checkpoint(client, project["id"])
    children, snapshot_cells = [], []
    versions = [(model_a, version_a), (model_a, version_a), (model_b, version_b), (model_b, version_b)]
    for ordinal, (model, version) in enumerate(versions):
        request_id = str(uuid4())
        payload = _payload(project["id"], model, version, request_id, f"prompt {ordinal}")
        payload["workflow"] = "grid"
        compiled = client.post("/api/generation-requests/compile", json=payload)
        assert compiled.status_code == 200, compiled.text
        children.append({"model_version_id": version["id"], "compiled_request_id": request_id,
            "admission_id": compiled.json()["admission"]["id"]})
        snapshot_cells.append({"id": f"cell-{ordinal}", "ordinal": ordinal, "x_index": ordinal % 2,
            "y_index": 0, "z_index": ordinal // 2, "x": {"id": f"x-{ordinal % 2}", "label": "duplicate"},
            "y": {"id": "y-0", "label": "1"}, "model_version_id": version["id"]})
    parent_id = str(uuid4())
    body = {"workflow": "grid", "provider": "fal", "context": {"kind": "project", "project_id": project["id"]},
        "model_version_ids": [version_a["id"], version_b["id"]], "compiled_request_id": parent_id,
        "client_request_id": parent_id, "request": {"workflow": "grid", "cells": snapshot_cells}, "children": children}
    queued = client.post("/api/generation-queue", json=body)
    assert queued.status_code == 202, queued.text
    assert queued.json()["model_version_ids"] == [version_a["id"], version_b["id"]]
    assert [child["model_version_id"] for child in queued.json()["children"]] == [version_a["id"], version_a["id"], version_b["id"], version_b["id"]]
    replay = client.post("/api/generation-queue", json=body)
    assert replay.status_code == 202 and replay.json()["id"] == queued.json()["id"]
    results = client.get(f"/api/generation-queue/{queued.json()['id']}/grid-results")
    assert results.status_code == 200
    assert [(cell["ordinal"], cell["x_index"], cell["z_index"], cell["x"]["label"]) for cell in results.json()["cells"]] == [
        (0, 0, 0, "duplicate"), (1, 1, 0, "duplicate"), (2, 0, 1, "duplicate"), (3, 1, 1, "duplicate")]
