from uuid import uuid4

from sqlalchemy import func, select

from titles_api import app as app_module, models
from titles_api.routers.generation_queue import queue_capability
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
