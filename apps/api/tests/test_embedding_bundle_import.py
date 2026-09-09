from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

from fastapi.testclient import TestClient


def _entry(path: Path, *, kind: str, model: str, object_key: str, provenance: dict | None = None) -> dict:
    payload = path.read_bytes()
    return {
        "kind": kind,
        "model": model,
        "source_path": str(path),
        "object_key": object_key,
        "sha256": sha256(payload).hexdigest(),
        "size": len(payload),
        "content_type": "image/png" if kind == "generated_image" else "application/octet-stream",
        "provenance": provenance or {},
    }


def _archive_entry(
    path: Path,
    *,
    kind: str,
    object_key: str,
    step: int | None = None,
    final: bool = False,
    metadata: dict | None = None,
) -> dict:
    payload = path.read_bytes()
    return {
        "kind": kind,
        "name": path.name,
        "logical_path": path.name,
        "source_path": str(path),
        "object_key": object_key,
        "sha256": sha256(payload).hexdigest(),
        "size": len(payload),
        "content_type": "image/png" if kind == "image" else "application/octet-stream",
        "step": step,
        "final": final,
        "artifact_format": "pytorch-embedding-v1" if kind == "embedding" else None,
        "method": "dsci" if kind == "embedding" else None,
        "tensor_metadata": {"shape": [5, 3584], "token_count": 5, "hidden_dimension": 3584} if kind == "embedding" else {},
        "metadata": metadata or {},
    }


def test_workspace_creation_clones_an_active_operator(client: TestClient):
    operator = client.get("/api/profiles/active").json()
    created = client.post("/api/workspaces", json={"name": "Embeddings"})

    assert created.status_code == 201
    workspace = created.json()
    assert workspace["name"] == "Embeddings"
    profiles = client.get("/api/profiles", headers={"X-Workspace-ID": workspace["id"]}).json()
    assert [(profile["display_name"], profile["initials"]) for profile in profiles] == [
        (operator["display_name"], operator["initials"]),
    ]


def test_embedding_bundle_import_registers_typed_artifact_images_and_backup_locations(
    client: TestClient,
    tmp_path: Path,
):
    workspace = client.post("/api/workspaces", json={"name": "Embeddings"}).json()
    headers = {"X-Workspace-ID": workspace["id"]}
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"title": "Qwen Image Embeddings", "description": "Recovered embedding experiments"},
    ).json()

    artifact = tmp_path / "airbrush.safetensors"
    artifact.write_bytes(b"small embedding tensor payload")
    image = tmp_path / "sample.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="))
    manifest = {
        "schema": "modelfiche.embedding-backup/v1",
        "bucket": "imagesets",
        "prefix": "embeddings/qwen-image/test",
        "models": {
            "airbrush-step8000": {
                "name": "Airbrush DSCI step 8000",
                "step": 8000,
                "base_model": "Qwen-Image",
                "base_model_revision": None,
                "artifact_type": "embedding",
                "artifact_format": "kef-qwen-dsci-v1",
                "method": "dsci",
                "tensor_metadata": {"shape": [5, 3584], "token_count": 5, "hidden_dimension": 3584},
                "compatibility": {"qwen-image": {"status": "observed"}},
                "notes": "Recovered historical artifact; immutable base revision was not recorded.",
            }
        },
        "files": [
            _entry(
                artifact,
                kind="embedding",
                model="airbrush-step8000",
                object_key="embeddings/qwen-image/test/artifact/airbrush.safetensors",
            ),
            _entry(
                image,
                kind="generated_image",
                model="airbrush-step8000",
                object_key="embeddings/qwen-image/test/images/sample.png",
                provenance={
                    "embedding": "airbrush.safetensors",
                    "prompt": "A portrait in an airbrush style",
                    "seed": 42,
                    "width": 1024,
                    "height": 1024,
                    "steps": 20,
                    "cfg": 4.0,
                },
            ),
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    request = {"project_id": project["id"], "manifest_path": str(manifest_path)}

    imported = client.post("/api/embedding-bundles/import", headers=headers, json=request)
    assert imported.status_code == 201, imported.text
    result = imported.json()
    assert result["imports"][0]["created"] is True
    assert result["imports"][0]["image_count"] == 1

    repeated = client.post("/api/embedding-bundles/import", headers=headers, json=request)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["imports"][0]["created"] is False
    assert repeated.json()["imports"][0]["image_count"] == 1

    imported_model = result["imports"][0]
    version = client.get(
        f"/api/model-versions/{imported_model['model_version_id']}",
        headers=headers,
    ).json()
    assert version["artifact_type"] == "embedding"
    assert version["artifact_format"] == "kef-qwen-dsci-v1"
    assert version["method"] == "dsci"
    assert version["compatibility"]["tensor_metadata"]["shape"] == [5, 3584]
    assert version["compatibility"]["base_model_revision"] is None
    assert version["readiness_summary"]["local"]["status"] == "hydrated"
    assert {location["provider"] for location in version["artifact"]["storage"]} == {"local", "s3"}

    gallery = client.get(
        "/api/gallery",
        headers=headers,
        params={"model_id": imported_model["model_id"], "kind": "image"},
    ).json()
    assert len(gallery) == 1
    assert gallery[0]["metadata"]["model_version_id"] == imported_model["model_version_id"]
    assert gallery[0]["metadata"]["artifact_type"] == "embedding"

    delivery = client.post(
        f"/api/assets/{gallery[0]['asset_revision_id']}/delivery",
        headers=headers,
        json={
            "asset_revision_id": gallery[0]["asset_revision_id"],
            "variant": {"kind": "content"},
        },
    )
    assert delivery.status_code == 200, delivery.text
    descriptor = delivery.json()
    assert descriptor["kind"] == "descriptor"
    delivered = client.get(descriptor["descriptor"]["delivery_url"], headers=headers)
    assert delivered.status_code == 200
    assert delivered.content == image.read_bytes()


def test_embedding_archive_import_preserves_run_lineage_and_is_idempotent(
    client: TestClient,
    tmp_path: Path,
):
    workspace = client.post("/api/workspaces", json={"name": "Embedding archive"}).json()
    headers = {"X-Workspace-ID": workspace["id"]}
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"title": "Qwen embedding archive"},
    ).json()
    checkpoint = tmp_path / "style_step1000.pt"
    checkpoint.write_bytes(b"historical embedding checkpoint")
    image = tmp_path / "preview.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="))
    report = tmp_path / "metrics.csv"
    report.write_text("step,loss\n0,1.0\n1000,0.2\n")
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"immutable source archive")
    manifest = {
        "schema": "modelfiche.embedding-experiment-archive/v1",
        "name": "Recovered Qwen experiments",
        "bucket": "imagesets",
        "prefix": "embeddings/qwen-image/archive",
        "runs": [{
            "key": "test/style",
            "family_key": "test",
            "name": "Style 1000",
            "base_model": "Qwen-Image",
            "method": "dsci",
            "config": {"token_count": 5},
            "checkpoints": [
                _archive_entry(
                    checkpoint,
                    kind="embedding",
                    object_key="embeddings/qwen-image/archive/objects/checkpoint",
                    step=1000,
                    final=True,
                )
            ],
            "images": [
                _archive_entry(
                    image,
                    kind="image",
                    object_key="embeddings/qwen-image/archive/objects/image",
                    step=1000,
                    metadata={"width": 1, "height": 1, "prompt": "A recovered preview", "seed": 7},
                )
            ],
            "evidence": [
                _archive_entry(
                    report,
                    kind="config",
                    object_key="embeddings/qwen-image/archive/objects/report",
                )
            ],
            "metric_samples": [{"step": 0, "loss": 1.0}, {"step": 1000, "loss": 0.2}],
            "metric_summary": {"loss": {"last": 0.2}},
        }],
        "unassigned": [
            _archive_entry(
                archive,
                kind="other",
                object_key="embeddings/qwen-image/archive/objects/archive",
            )
        ],
    }
    manifest_path = tmp_path / "archive-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    request = {"project_id": project["id"], "manifest_path": str(manifest_path)}

    imported = client.post("/api/embedding-archives/import", headers=headers, json=request)
    assert imported.status_code == 201, imported.text
    result = imported.json()
    assert result["imports"][0]["created"] is True
    assert result["imports"][0]["checkpoint_count"] == 1
    assert result["imports"][0]["image_count"] == 1
    assert result["imports"][0]["evidence_count"] == 1
    assert result["unassigned_count"] == 1

    repeated = client.post("/api/embedding-archives/import", headers=headers, json=request)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["imports"][0]["created"] is False
    assert repeated.json()["unassigned_count"] == 0

    run_id = result["imports"][0]["run_id"]
    run = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert run["name"] == "Style 1000"
    assert run["current_step"] == 1000
    assert len(run["checkpoints"]) == 1
    metrics = client.get(f"/api/runs/{run_id}/metrics", headers=headers).json()
    assert [(row["step"], row["value"]) for row in metrics["points"]] == [(0, 1.0), (1000, 0.2)]
    assets = client.get(
        "/api/assets",
        headers=headers,
        params={"project_id": project["id"], "limit": 20},
    ).json()
    assert len(assets) == 4
    assert all(any(location["provider"] == "local" for location in asset["locations"]) for asset in assets)
    version = client.get(
        f"/api/model-versions/{result['imports'][0]['model_version_id']}",
        headers=headers,
    ).json()
    assert version["artifact_type"] == "embedding"
    assert version["compatibility"]["import"]["family_key"] == "test"
