from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from titles_api import models, schemas
from titles_api.checkpoint_revisions import establish_checkpoint_revision
from titles_api.database import get_db
from titles_api.merge_operations import compact_notation


def _project(client: TestClient):
    profile = client.get("/api/profiles").json()[0]
    headers = {"X-Profile-ID": profile["id"]}
    project = client.post(
        "/api/projects", headers=headers, json={"title": "Composition project"}
    ).json()
    return headers, project


def _dataset(client: TestClient, headers: dict[str, str], project: dict):
    assets = [
        client.post(
            "/api/assets",
            headers=headers,
            json={
                "project_id": project["id"],
                "kind": "image",
                "name": f"image-{index}.png",
            },
        ).json()
        for index in range(2)
    ]
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "Composed dataset",
            "trigger_words": ["shared-token"],
            "items": [
                {"asset_id": asset["id"], "caption": f"caption {index}"}
                for index, asset in enumerate(assets)
            ],
        },
    ).json()
    version = client.get(f"/api/datasets/{dataset['id']}/versions").json()[0]
    return dataset, version


def test_composed_version_requires_one_primary_membership_and_changes_digest(
    client: TestClient,
):
    headers, project = _project(client)
    dataset, version = _dataset(client, headers, project)
    base_subsets = client.get(f"/api/dataset-versions/{version['id']}/subsets").json()
    assert [(row["key"], row["item_count"]) for row in base_subsets] == [("default", 2)]

    draft_id = client.post(
        f"/api/datasets/{dataset['id']}/drafts",
        headers=headers,
        json={"base_version_id": version["id"]},
    ).json()["id"]
    draft = client.get(f"/api/dataset-drafts/{draft_id}").json()
    default_subset = draft["subsets"][0]
    assert (
        client.delete(
            f"/api/dataset-drafts/{draft_id}/subsets/{default_subset['id']}",
            headers=headers,
        ).status_code
        == 204
    )
    first = client.post(
        f"/api/dataset-drafts/{draft_id}/subsets",
        headers=headers,
        json={"key": "first", "name": "First", "role": "style"},
    ).json()
    second = client.post(
        f"/api/dataset-drafts/{draft_id}/subsets",
        headers=headers,
        json={"key": "second", "name": "Second", "role": "style"},
    ).json()
    draft_items = client.get(f"/api/dataset-drafts/{draft_id}").json()["items"]
    client.post(
        f"/api/dataset-drafts/{draft_id}/subset-assignments",
        headers=headers,
        json={
            "subset_id": first["id"],
            "draft_item_ids": [draft_items[0]["id"]],
            "membership_role": "primary",
        },
    )

    rejected = client.post(
        f"/api/dataset-drafts/{draft_id}/publish",
        headers=headers,
        json={"name": "Incomplete composition"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["item_ids"] == [draft_items[1]["id"]]

    assigned = client.post(
        f"/api/dataset-drafts/{draft_id}/subset-assignments",
        headers=headers,
        json={
            "subset_id": second["id"],
            "draft_item_ids": [draft_items[1]["id"]],
            "membership_role": "primary",
        },
    )
    assert assigned.status_code == 200
    draft_with_subsets = client.get(f"/api/dataset-drafts/{draft_id}").json()
    assert [item["subdatasets"][0]["name"] for item in draft_with_subsets["items"]] == ["First", "Second"]
    published = client.post(
        f"/api/dataset-drafts/{draft_id}/publish",
        headers=headers,
        json={"name": "Two styles"},
    )
    assert published.status_code == 201
    composed = published.json()
    assert composed["content_digest"] != version["content_digest"]
    subsets = client.get(f"/api/dataset-versions/{composed['id']}/subsets").json()
    assert [(row["key"], row["item_count"]) for row in subsets] == [
        ("first", 1),
        ("second", 1),
    ]
    ordered = client.get(
        f"/api/dataset-versions/{composed['id']}/items",
        params={"paginated": True, "sort": "subdataset"},
    ).json()
    assert ordered["total"] == 2
    assert [item["subdatasets"][0]["name"] for item in ordered["items"]] == ["First", "Second"]
    filtered = client.get(
        f"/api/dataset-versions/{composed['id']}/items",
        params={"paginated": True, "subset_id": next(row["id"] for row in subsets if row["key"] == "second"), "sort": "subdataset"},
    ).json()
    assert [item["subdatasets"][0]["key"] for item in filtered["items"]] == ["second"]


def test_training_run_composes_independent_dataset_versions(client: TestClient):
    headers, project = _project(client)
    _, first_version = _dataset(client, headers, project)
    _, second_version = _dataset(client, headers, project)
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "dataset_version_id": first_version["id"],
            "name": "Multi-dataset training",
            "status": "pending",
        },
    ).json()

    attached = client.post(
        f"/api/runs/{run['id']}/dataset-inputs",
        json={
            "dataset_version_id": second_version["id"],
            "alias": "specialist",
            "sampling_weight": 0.4,
            "repeat_count": 2,
            "position": 1,
        },
    )
    assert attached.status_code == 201, attached.text
    input_id = attached.json()["id"]
    inputs = client.get(f"/api/runs/{run['id']}/dataset-inputs").json()
    assert [(row["alias"], row["item_count_snapshot"]) for row in inputs] == [
        (None, 2),
        ("specialist", 2),
    ]

    updated = client.patch(
        f"/api/runs/{run['id']}/dataset-inputs/{input_id}",
        json={
            "alias": "render-specialist",
            "sampling_weight": 0.65,
            "repeat_count": 3,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["alias"] == "render-specialist"
    assert updated.json()["sampling_weight"] == 0.65
    assert updated.json()["repeat_count"] == 3

    assert (
        client.delete(f"/api/runs/{run['id']}/dataset-inputs/{input_id}").status_code
        == 204
    )
    assert len(client.get(f"/api/runs/{run['id']}/dataset-inputs").json()) == 1


def _checkpoint_source(
    client: TestClient,
    headers: dict[str, str],
    project: dict,
    alias: str,
    digest: str,
    step: int,
):
    asset = client.post(
        "/api/assets",
        headers=headers,
        json={
            "project_id": project["id"],
            "kind": "model",
            "name": f"{alias}.safetensors",
            "sha256": digest,
            "location": {
                "provider": "s3",
                "uri": f"s3://fixture/{alias}.safetensors",
                "bucket": "fixture",
                "object_key": f"{alias}.safetensors",
            },
        },
    ).json()
    run = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": f"{alias} training",
            "trainer": "ai-toolkit",
            "base_model": "krea/Krea-2-Raw",
            "status": "completed",
        },
    ).json()
    checkpoint = client.post(
        f"/api/runs/{run['id']}/checkpoints",
        headers=headers,
        json={"step": step, "asset_id": asset["id"], "state": "available"},
    ).json()
    session_scope = client.app.dependency_overrides[get_db]()
    session = next(session_scope)
    try:
        location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset["id"]
            )
        )
        location.verification_state = "verified"
        location.verified_sha256 = digest
        revision = establish_checkpoint_revision(
            session, session.get(models.Checkpoint, checkpoint["id"])
        )
        session.commit()
        return revision.id
    finally:
        session_scope.close()


def test_merge_prepare_complete_is_atomic_and_idempotent(client: TestClient):
    headers, project = _project(client)
    full_sha = "1" * 64
    render_sha = "2" * 64
    full_revision = _checkpoint_source(client, headers, project, "full", full_sha, 2400)
    render_revision = _checkpoint_source(
        client, headers, project, "render", render_sha, 3000
    )
    recipe = {
        "$schema": "modelfiche.checkpoint-merge/v1",
        "name": "Managed render coverage",
        "operator": "layer_weighted_sum",
        "base_model": "krea/Krea-2-Raw",
        "inputs": [
            {
                "alias": "full",
                "checkpoint_revision_id": full_revision,
                "sha256": full_sha,
                "step": 2400,
                "rank": 32,
                "role": "general",
                "weights": {"text_fusion": 0.5, "transformer": 0.3},
            },
            {
                "alias": "render",
                "checkpoint_revision_id": render_revision,
                "sha256": render_sha,
                "step": 3000,
                "rank": 64,
                "role": "specialist",
                "key_format": "ai_toolkit",
                "weights": {"text_fusion": 0.5, "transformer": 0.7},
            },
        ],
        "output": {"rank": 96, "dtype": "float16"},
    }
    request = {
        "project_id": project["id"],
        "client_request_id": "agent-merge-1",
        "recipe": recipe,
        "registration": {
            "version_name": "Render coverage",
            "trigger_words": ["kzapata"],
        },
    }
    prepared = client.post(
        "/api/merge-operations/prepare", headers=headers, json=request
    )
    assert prepared.status_code == 201, prepared.text
    operation = prepared.json()
    repeated = client.post(
        "/api/merge-operations/prepare", headers=headers, json=request
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == operation["id"]
    assert operation["notation"].startswith("layer-weighted-sum[r96]")

    output = Path(operation["output_directory"]) / "managed-output.safetensors"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"verified merge fixture")
    output_sha = sha256(output.read_bytes()).hexdigest()
    complete_body = {
        "output_path": str(output),
        "output_sha256": output_sha,
        "output_size": output.stat().st_size,
        "output_step": 3000,
    }
    completed = client.post(
        f"/api/merge-operations/{operation['id']}/complete",
        headers=headers,
        json=complete_body,
    )
    assert completed.status_code == 200
    result = completed.json()
    assert result["status"] == "completed"
    assert result["output"]["checkpoint_revision_id"]
    assert result["output"]["model_version_id"]

    repeated_completion = client.post(
        f"/api/merge-operations/{operation['id']}/complete",
        headers=headers,
        json=complete_body,
    )
    assert repeated_completion.status_code == 200
    assert repeated_completion.json()["output"] == result["output"]
    lineage = client.get(
        "/api/lineage",
        params={
            "subject_type": "checkpoint_revision",
            "subject_id": result["output"]["checkpoint_revision_id"],
            "depth": 3,
        },
    ).json()
    merged_edges = [row for row in lineage if row.get("relationship") == "merged_into"]
    assert len(merged_edges) == 2


def test_v2_merge_accepts_workspace_sources_and_records_method_notation(
    client: TestClient,
):
    headers, output_project = _project(client)
    donor_project = client.post(
        "/api/projects", headers=headers, json={"title": "Donor project"}
    ).json()
    anchor_sha = "3" * 64
    donor_sha = "4" * 64
    anchor_revision = _checkpoint_source(
        client, headers, output_project, "fkaor", anchor_sha, 3000
    )
    donor_revision = _checkpoint_source(
        client, headers, donor_project, "qhoov", donor_sha, 3750
    )
    recipe = {
        "$schema": "modelfiche.checkpoint-merge/v2",
        "name": "Fujiwara Kaoru x Hoover cosine anchor",
        "base_model": "krea/Krea-2-Raw",
        "inputs": [
            {
                "alias": "fkaor",
                "checkpoint_revision_id": anchor_revision,
                "sha256": anchor_sha,
                "step": 3000,
                "rank": 32,
                "role": "primary",
                "key_format": "ai_toolkit",
            },
            {
                "alias": "qhoov",
                "checkpoint_revision_id": donor_revision,
                "sha256": donor_sha,
                "step": 3750,
                "rank": 32,
                "role": "donor",
                "key_format": "ai_toolkit",
            },
        ],
        "method": {
            "kind": "cosine_gated",
            "anchor": "fkaor",
            "donor": "qhoov",
            "parameters": {
                "donor_min": 0.15,
                "donor_max": 0.55,
                "lower_percentile": 5,
                "upper_percentile": 95,
            },
        },
        "output": {"rank": 64, "dtype": "float16", "factorization": "exact_concat"},
    }

    prepared = client.post(
        "/api/merge-operations/prepare",
        headers=headers,
        json={
            "project_id": output_project["id"],
            "client_request_id": "agent-v2-cosine",
            "recipe": recipe,
            "registration": {
                "version_name": recipe["name"],
                "trigger_words": ["fkaor", "qhoov"],
            },
        },
    )

    assert prepared.status_code == 201, prepared.text
    operation = prepared.json()
    assert operation["schema_version"] == "modelfiche.checkpoint-merge/v2"
    assert operation["operator"] == "cosine_gated"
    assert operation["notation"].startswith("cosine-gated[r64]")
    assert "anchor=fkaor@3000" in operation["notation"]
    assert {item["checkpoint_revision_id"] for item in operation["inputs"]} == {
        anchor_revision,
        donor_revision,
    }


def test_modelfiche_prepares_multi_source_slerp_with_scoped_and_automatic_weights(
    client: TestClient,
):
    headers, output_project = _project(client)
    donor_project = client.post(
        "/api/projects", headers=headers, json={"title": "Spherical donors"}
    ).json()
    source_specs = [
        ("illingworth", output_project, "5" * 64, 2500),
        ("hoover", donor_project, "6" * 64, 3750),
        ("fujiwara", donor_project, "7" * 64, 3000),
    ]
    revisions = {
        alias: _checkpoint_source(client, headers, project, alias, digest, step)
        for alias, project, digest, step in source_specs
    }
    recipe = {
        "$schema": "modelfiche.checkpoint-merge/v2",
        "name": "Illingworth three-way spherical merge",
        "base_model": "krea/Krea-2-Raw",
        "inputs": [
            {
                "alias": alias,
                "checkpoint_revision_id": revisions[alias],
                "sha256": digest,
                "step": step,
                "rank": 32,
                "key_format": "ai_toolkit",
            }
            for alias, _, digest, step in source_specs
        ],
        "method": {
            "kind": "slerp",
            "anchor": "illingworth",
            "donors": ["hoover", "fujiwara"],
            "parameters": {
                "weights": {"illingworth": -0.1, "hoover": 0.7, "fujiwara": 0.4},
                "text_fusion_weights": "auto",
                "transformer_weights": {
                    "illingworth": 0.5,
                    "hoover": 0.3,
                    "fujiwara": 0.2,
                },
                "transformer_blocks": [
                    {
                        "start": 0,
                        "end": 7,
                        "weights": {
                            "illingworth": 0.6,
                            "hoover": 0.25,
                            "fujiwara": 0.15,
                        },
                    }
                ],
            },
        },
        "output": {"rank": 96, "dtype": "float16", "factorization": "exact_concat"},
    }

    prepared = client.post(
        "/api/merge-operations/prepare",
        headers=headers,
        json={
            "project_id": output_project["id"],
            "client_request_id": "multi-source-slerp",
            "recipe": recipe,
            "registration": {"version_name": recipe["name"]},
        },
    )

    assert prepared.status_code == 201, prepared.text
    operation = prepared.json()
    assert operation["operator"] == "slerp"
    assert len(operation["inputs"]) == 3
    assert operation["recipe"]["method"]["parameters"]["text_fusion_weights"] == "auto"
    assert "auto(balanced-energy)" in operation["notation"]
    assert "b00-07" in operation["notation"]


def test_geometric_merge_methods_validate_and_render_notation():
    recipe = {
        "$schema": "modelfiche.checkpoint-merge/v2",
        "name": "Hoover geometric merge",
        "base_model": "krea/Krea-2-Raw",
        "inputs": [
            {
                "alias": "qhoov",
                "checkpoint_revision_id": "qhoov-revision",
                "sha256": "3" * 64,
                "step": 3750,
                "rank": 32,
            },
            {
                "alias": "fkaor",
                "checkpoint_revision_id": "fkaor-revision",
                "sha256": "4" * 64,
                "step": 3000,
                "rank": 32,
            },
        ],
        "method": {
            "kind": "norm_balanced",
            "anchor": "qhoov",
            "donor": "fkaor",
            "parameters": {"anchor_weight": 0.6, "donor_weight": 0.4},
        },
        "output": {"rank": 64, "dtype": "float16", "factorization": "exact_concat"},
    }
    norm_balanced = schemas.MergeRecipe.model_validate(recipe)
    assert compact_notation(norm_balanced).startswith(
        "norm-balanced[r64] { qhoov@3750 0.60 + fkaor@3000 0.40 }"
    )

    recipe["method"] = {
        "kind": "slerp",
        "anchor": "qhoov",
        "donor": "fkaor",
        "parameters": {"donor_weight": 0.4},
    }
    spherical = schemas.MergeRecipe.model_validate(recipe)
    assert compact_notation(spherical).startswith(
        "slerp[r64] { qhoov@3750 0.60 ↝ fkaor@3000 0.40 }"
    )
