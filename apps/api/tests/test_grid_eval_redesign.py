from titles_api import app as app_module, models
import pytest

from titles_api.experiment_plans import resolve_plan
from titles_api.routers.grid_eval import _hydrate_input
from fastapi import HTTPException


def plan(*, targets=None, shared_params=None):
    return {
        "contract_version": "2026-07-22.v1",
        "axes": {
            "x": {"name": "seed", "values": [1, 2]},
            "y": {"name": "image_size", "values": ["square_hd", "portrait_4_3"]},
            "z": None,
        },
        "cases": [{"case_id": "case-a", "input": {"prompt": "a"}}],
        "targets": targets or [{"target_id": "ideogram", "provider": "fal", "endpoint_id": "ideogram/v4/lora"}],
        "shared_params": shared_params or {},
    }


def test_preflight_is_axis_first_and_exactly_2x2():
    result = resolve_plan(plan(), "project-1")
    assert result["valid"]
    assert result["request_count"] == 4
    assert [cell["ordinal"] for cell in result["cells"]] == [0, 1, 2, 3]


def test_preflight_applies_lora_scale_axis_to_registered_lora():
    raw = plan(targets=[{
        "target_id": "krea",
        "provider": "fal",
        "endpoint_id": "fal-ai/krea-2/turbo/lora",
        "_server_hydrated": True,
        "registered_lora": {"path": "https://fal.example/checkpoint.safetensors", "scale": 1},
    }])
    raw["axes"] = {
        "x": {"name": "seed", "values": [42]},
        "y": {"name": "lora_scale", "values": [0.8, 1, 1.2]},
        "z": None,
    }

    result = resolve_plan(raw, "project-1")

    assert result["valid"]
    assert [cell["effective_params"]["loras"][0]["scale"] for cell in result["cells"]] == [0.8, 1.0, 1.2]


def test_preflight_mixes_endpoints_and_allows_endpoint_only_targets():
    raw = plan(targets=[
        {"target_id": "ideo", "provider": "fal", "endpoint_id": "ideogram/v4/lora"},
        {"target_id": "krea", "provider": "fal", "endpoint_id": "fal-ai/krea-2/turbo/lora"},
    ])
    raw["axes"] = {
        "x": {"name": "target", "values": ["ideo", "krea"]},
        "y": {"name": "seed", "values": [1, 2]},
        "z": None,
    }
    result = resolve_plan(raw, "project-1")
    assert result["valid"]
    assert result["request_count"] == 4
    assert {cell["target"]["endpoint_id"] for cell in result["cells"]} == {"ideogram/v4/lora", "fal-ai/krea-2/turbo/lora"}
    assert all(cell["target"]["model_version_id"] is None for cell in result["cells"])


@pytest.mark.parametrize("payload", [
    {"targets": [{"target_id": "t", "endpoint_id": "ideogram/v4/lora", "_server_hydrated": True}]},
    {"targets": [{"target_id": "t", "endpoint_id": "ideogram/v4/lora", "registered_lora": {"path": "spoof"}}]},
    {"shared_params": {"loras": [{"path": "spoof"}]}},
    {"axes": {"x": {"name": "loras", "values": [1]}, "y": {"name": "seed", "values": [1]}}},
])
def test_client_cannot_inject_server_owned_lora_fields(payload):
    raw = plan()
    raw.update(payload)
    with pytest.raises(HTTPException) as exc:
        _hydrate_input(None, "project-1", raw)
    assert exc.value.status_code == 422


def test_plan_digest_is_stable_and_excludes_server_plan_id():
    first = resolve_plan(plan(), "project-1")
    second = resolve_plan({**plan(), "plan_id": "different-server-id"}, "project-1")
    assert first["plan"]["digest"] == second["plan"]["digest"]
    assert "plan_id" not in first["plan"]

def test_queue_requires_exact_billing_acknowledgement():
    from pydantic import ValidationError
    from titles_api.schemas import GridQueueRequest

    base = {"idempotency_key": "q", "plan_version": 1, "plan_digest": "a" * 64}
    with pytest.raises(ValidationError):
        GridQueueRequest(**base)
    with pytest.raises(ValidationError):
        GridQueueRequest(**base, billing_acknowledgement="yes")
    request = GridQueueRequest(**base, billing_acknowledgement="I understand FAL may bill this run even if checkpoint fetch fails")
    assert request.billing_acknowledgement.startswith("I understand FAL")

def test_endpoint_only_mixed_plan_api_smoke(client):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Grid smoke"}).json()
    project_id = project["id"]
    plan = {
        "contract_version": "2026-07-22.v1",
        "axes": {
            "x": {"name": "target", "values": ["ideo", "krea"]},
            "y": {"name": "seed", "values": [1, 2]},
            "z": None,
        },
        "cases": [{"case_id": "case-a", "ordinal": 0, "input": {"prompt": "a"}}],
        "targets": [
            {"target_id": "ideo", "ordinal": 0, "provider": "fal", "endpoint_id": "ideogram/v4/lora"},
            {"target_id": "krea", "ordinal": 1, "provider": "fal", "endpoint_id": "fal-ai/krea-2/turbo/lora"},
        ],
        "shared_params": {},
    }
    preflight = client.post(f"/api/projects/{project_id}/experiment-plans/preflight", json={**plan, "idempotency_key": "pf"})
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["request_count"] == 4
    create = client.post(f"/api/projects/{project_id}/grids", json={"name": "Smoke", "idempotency_key": "create", "plan": plan})
    assert create.status_code == 201, create.text
    grid = create.json()
    assert len(grid["cells"]) == 4
    assert {cell["y_index"] for cell in grid["cells"]} == {0, 1}
    retry = client.post(f"/api/projects/{project_id}/grids", json={"name": "Smoke", "idempotency_key": "create", "plan": plan})
    assert retry.status_code == 201
    assert retry.json()["id"] == grid["id"]
    queue_body = {
        "idempotency_key": "queue",
        "plan_version": grid["plan_version"],
        "plan_digest": grid["plan_digest"],
        "billing_acknowledgement": "I understand FAL may bill this run even if checkpoint fetch fails",
    }
    queued = client.post(f"/api/projects/{project_id}/grids/{grid['id']}/queue", json=queue_body)
    assert queued.status_code == 202, queued.text
    queued_retry = client.post(f"/api/projects/{project_id}/grids/{grid['id']}/queue", json=queue_body)
    assert queued_retry.status_code == 202
    assert queued_retry.json()["admission_id"] == queued.json()["admission_id"]
    with app_module.SessionLocal.begin() as session:
        run = session.get(models.EvalRun, queued.json()["run_id"])
        run.status = "succeeded"
        assert session.query(models.GenerationQueueChild).filter_by(queue_item_id=queued.json()["admission_id"]).count() == 4
    assessment = client.post(f"/api/projects/{project_id}/evals", json={"name": "Review", "idempotency_key": "eval", "run_id": queued.json()["run_id"], "assessment": {"kind": "human_review"}})
    assert assessment.status_code == 201, assessment.text



def test_attach_existing_asset_persists_grid_and_comfyui_metadata_idempotently(client):
    project = client.post("/api/projects", json={"title": "ComfyUI attachment"}).json()
    asset = client.post(
        "/api/assets",
        json={
            "project_id": project["id"],
            "kind": "image",
            "name": "comfyui-output.png",
            "mime_type": "image/png",
            "metadata": {"prompt": "a portrait of a woman"},
        },
    ).json()
    grid = client.post(
        f"/api/projects/{project['id']}/grids",
        json={"name": "Imported output", "idempotency_key": "grid-create", "plan": plan()},
    ).json()
    body = {
        "asset_id": asset["id"],
        "idempotency_key": "comfyui-cell-0",
        "provider": "comfyui",
        "workflow": {
            "name": "Krea 2 ComfyUI API workflow",
            "url": "https://workflow.example/krea2.json",
        },
        "seed": 42,
        "metadata": {"sampler": "euler"},
    }

    attached = client.post(
        f"/api/projects/{project['id']}/grids/{grid['id']}/cells/{grid['cells'][0]['id']}/attach",
        json=body,
    )
    assert attached.status_code == 201, attached.text
    payload = attached.json()
    assert payload["asset_id"] == asset["id"]
    assert payload["provider"] == "comfyui"
    assert payload["workflow"]["name"] == "Krea 2 ComfyUI API workflow"
    assert payload["status"] == "succeeded"

    retry = client.post(
        f"/api/projects/{project['id']}/grids/{grid['id']}/cells/{grid['cells'][0]['id']}/attach",
        json=body,
    )
    assert retry.status_code == 201
    assert retry.json() == payload

    conflict = client.post(
        f"/api/projects/{project['id']}/grids/{grid['id']}/cells/{grid['cells'][0]['id']}/attach",
        json={**body, "seed": 43},
    )
    assert conflict.status_code == 409

    with app_module.SessionLocal() as session:
        stored_asset = session.get(models.Asset, asset["id"])
        stored_cell = session.get(models.GridCell, grid["cells"][0]["id"])
        stored_output = session.get(models.EvalOutput, payload["eval_output_id"])
        assert stored_asset.metadata_["provider"] == "comfyui"
        assert stored_asset.metadata_["generated_by"] == "comfyui"
        assert stored_asset.metadata_["workflow"]["url"] == "https://workflow.example/krea2.json"
        assert stored_asset.metadata_["sampler"] == "euler"
        assert stored_cell.status == "succeeded"
        assert stored_cell.eval_output_id == stored_output.id
        assert stored_output.provider_metadata["source"] == "existing_asset"
        assert stored_output.provider_metadata["grid_metadata"]["ordinal"] == 0
        assert session.query(models.Job).filter_by(kind="grid.attach_existing_asset").count() == 1

    gallery = client.get("/api/gallery", params={"project_id": project["id"], "category": "eval_output"})
    assert gallery.status_code == 200
    gallery_items = gallery.json()
    gallery_asset = next(item for item in gallery_items if item["id"] == asset["id"])
    assert gallery_asset["metadata"]["workflow"]["url"] == "https://workflow.example/krea2.json"