from fastapi.testclient import TestClient


def _workspace_header(workspace_reference: str) -> dict[str, str]:
    return {"X-Workspace-ID": workspace_reference}


def test_entity_resolution_and_mutations_fail_closed_across_workspaces(
    client: TestClient,
):
    primary = client.get("/api/workspaces").json()[0]
    project = client.post("/api/projects", json={"title": "Primary project"}).json()
    dataset = client.post(
        "/api/datasets",
        json={"project_id": project["id"], "name": "Primary dataset", "items": []},
    ).json()
    run = client.post(
        "/api/runs",
        json={"project_id": project["id"], "name": "Primary run"},
    ).json()
    other = client.post("/api/workspaces", json={"name": "Other"}).json()
    other_headers = _workspace_header(other["slug"])

    for entity_type, entity_id in (
        ("project", project["id"]),
        ("dataset", dataset["id"]),
        ("run", run["id"]),
    ):
        resolution = client.get(
            f"/api/workspace-resolutions/{entity_type}/{entity_id}",
            headers=other_headers,
        )
        assert resolution.status_code == 200
        assert resolution.json()["workspace_id"] == primary["id"]
        assert resolution.json()["workspace_slug"] == primary["slug"]

    project_update = client.patch(
        f"/api/projects/{project['id']}",
        headers=other_headers,
        json={"title": "Wrong workspace"},
    )
    dataset_create = client.post(
        "/api/datasets",
        headers=other_headers,
        json={"project_id": project["id"], "name": "Wrong workspace", "items": []},
    )
    run_create = client.post(
        "/api/runs",
        headers=other_headers,
        json={"project_id": project["id"], "name": "Wrong workspace"},
    )

    for response in (project_update, dataset_create, run_create):
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "workspace_entity_mismatch",
            "message": "Project belongs to another workspace",
            "declared_workspace_id": other["id"],
            "actual_workspace_id": primary["id"],
            "entity_type": "Project",
            "entity_id": project["id"],
        }


def test_workspace_slugs_are_readable_unique_and_selectable(client: TestClient):
    first = client.post("/api/workspaces", json={"name": "Creative Studio"}).json()
    second = client.post("/api/workspaces", json={"name": "Creative Studio"}).json()

    assert first["slug"] == "creative-studio"
    assert second["slug"] == "creative-studio-2"

    project = client.post(
        "/api/projects",
        headers=_workspace_header(second["slug"]),
        json={"title": "Slug-scoped project"},
    )

    assert project.status_code == 201
    assert project.json()["workspace_id"] == second["id"]


def test_api_mutations_require_explicit_workspace(client: TestClient):
    response = client.post(
        "/api/projects",
        headers={"X-Workspace-ID": ""},
        json={"title": "Implicit project"},
    )

    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "workspace_required"
