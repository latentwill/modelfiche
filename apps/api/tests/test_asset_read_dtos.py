from fastapi.testclient import TestClient
from titles_api import app as app_module
from titles_api import models


def _seed_image_dataset(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post("/api/projects", headers=headers, json={"title": "Image DTO project"}).json()
    asset = client.post(
        "/api/assets",
        headers=headers,
        json={"project_id": project["id"], "kind": "image", "name": "revision.png"},
    ).json()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"project_id": project["id"], "name": "Revision images", "items": [{"asset_id": asset["id"]}]},
    ).json()
    return asset, dataset


def test_image_read_dtos_expose_asset_id_as_asset_revision_id(client: TestClient):
    asset, dataset = _seed_image_dataset(client)

    assert client.get("/api/gallery").json() == []
    gallery_row = client.get("/api/gallery?include_dataset_assets=true").json()[0]
    asset_row = client.get(f"/api/assets/{asset['id']}").json()
    metadata = client.get(f"/api/assets/{asset['id']}/metadata").json()
    context = client.get(f"/api/assets/{asset['id']}/context").json()
    version_id = client.get(f"/api/datasets/{dataset['id']}").json()["current_version_id"]
    version_item = client.get(f"/api/dataset-versions/{version_id}/items").json()[0]
    draft_id = client.post(f"/api/datasets/{dataset['id']}/drafts", json={"base_version_id": version_id}).json()["id"]
    draft_item = client.get(f"/api/dataset-drafts/{draft_id}").json()["items"][0]

    assert gallery_row["asset_revision_id"] == asset["id"]
    assert asset_row["asset_revision_id"] == asset["id"]
    assert metadata["asset_revision_id"] == asset["id"]
    assert context["asset_revision_id"] == asset["id"]
    assert version_item["asset_revision_id"] == asset["id"]
    assert version_item["asset_metadata"] == {}
    assert draft_item["asset_revision_id"] == asset["id"]

def test_eval_and_grid_image_read_dtos_expose_asset_revision_id(client: TestClient):
    asset, _dataset = _seed_image_dataset(client)

    with app_module.SessionLocal.begin() as db:
        prompts = models.PromptSet(project_id=asset["project_id"], name="DTO prompts")
        db.add(prompts)
        db.flush()
        prompt = models.Prompt(prompt_set_id=prompts.id, text="portrait", position=0)
        db.add(prompt)
        definition = models.EvalDefinition(
            project_id=asset["project_id"],
            name="DTO evaluation",
            endpoint="test/endpoint",
            prompt_set_id=prompts.id,
        )
        db.add(definition)
        db.flush()
        run = models.EvalRun(definition_id=definition.id, status="succeeded")
        db.add(run)
        db.flush()
        output = models.EvalOutput(eval_run_id=run.id, prompt_id=prompt.id, asset_id=asset["id"], seed=7)
        db.add(output)
        db.flush()
        grid = models.GridDefinition(
            project_id=asset["project_id"],
            name="DTO grid",
            eval_definition_id=definition.id,
            x_axis={"values": ["x"]},
            y_axis={"values": ["y"]},
        )
        db.add(grid)
        db.flush()
        db.add(models.GridCell(grid_definition_id=grid.id, eval_output_id=output.id, x_index=0, y_index=0))

    detail = client.get(f"/api/eval-runs/{run.id}").json()
    outputs = client.get(f"/api/eval-runs/{run.id}/outputs").json()
    grid = client.get(f"/api/grid-definitions/{grid.id}").json()
    project_grid = client.get(f"/api/projects/{asset['project_id']}/grids/{grid['id']}").json()

    assert detail["outputs"][0]["asset_revision_id"] == asset["id"]
    assert outputs[0]["asset_revision_id"] == asset["id"]
    assert grid["cells"][0]["asset_revision_id"] == asset["id"]
    assert project_grid["cells"][0]["asset_revision_id"] == asset["id"]
