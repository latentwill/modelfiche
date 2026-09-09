from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from titles_api import app as app_module, models
from titles_api.storage.gates import GateEvidence, StorageGateStore


def _session():
    return app_module.SessionLocal()


def _enable_migration_gate() -> None:
    with _session() as db:
        raw = db.connection().connection
        gates = StorageGateStore(raw)
        foundation = gates.get("storage_contract")
        gates.verify_feature("storage_migration", foundation.verification_receipt_id, "u8-test-evidence")


def test_policy_patch_projects_write_ready_source_and_rejects_stale_version(client: TestClient):
    with _session() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="S3 writable",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["managed/"],
            is_active=True,
        )
        db.add(source)
        db.flush()
        db.add(
            models.SourceCapability(
                source_id=source.id,
                source_fingerprint="source-v1:test",
                adapter_version="test",
                boot_id="boot",
                credential_epoch="epoch",
                capability="managed_write",
                state="ready",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        db.commit()
        source_id = source.id

    response = client.patch(
        "/api/storage-policy",
        json={"expected_version": 1, "desired_provider": "s3", "write_source_id": source_id, "fal_local_fallback": True},
    )
    assert response.status_code == 200
    assert response.json()["resolved_provider"] == "s3"
    assert response.json()["version"] == 1
    assert response.json()["announcement"]["mode"] == "assertive"

    stale = client.patch(
        "/api/storage-policy",
        json={"expected_version": 0, "desired_provider": "local", "fal_local_fallback": True},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "storage_policy_conflict"
    assert stale.json()["detail"]["current"]["version"] == 1

    with _session() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        policy = db.scalar(select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == workspace.id))
        policy.desired_provider = "s3"
        policy.write_source_id = None
        policy.selection_origin = "automatic"
        db.commit()
    automatic = client.get("/api/storage-policy")
    assert automatic.status_code == 200
    assert automatic.json()["resolved_provider"] == "s3"
    assert automatic.json()["write_source_id"] == source_id


def test_migration_preview_admit_detail_and_cancel_preserve_owner_and_counts(client: TestClient):
    with _session() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        old_root = models.LocalRoot(
            workspace_id=workspace.id,
            kind="asset",
            canonical_path="/tmp/u8-old-assets",
            fingerprint="device:old",
            owner_uid=501,
            mode=0o700,
            availability="available",
        )
        root = models.LocalRoot(
            workspace_id=workspace.id,
            kind="asset",
            canonical_path="/tmp/u8-assets",
            fingerprint="device:inode",
            owner_uid=501,
            mode=0o700,
            availability="available",
        )
        asset = models.Asset(workspace_id=workspace.id, kind=models.AssetKind.image, name="one.png")
        db.add_all([old_root, root, asset])
        db.flush()
        db.add(
            models.AssetLocation(
                asset_id=asset.id,
                workspace_id=workspace.id,
                provider="local",
                uri="local://old/one.png",
                local_root_id=old_root.id,
                relative_path="one.png",
                size=12,
                verified_size=12,
                verified_sha256="a" * 64,
                verification_state="available",
                last_verified_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        root_id = root.id
    _enable_migration_gate()

    preview = client.post(
        "/api/storage-migrations/preview",
        json={
            "scope": {"kind": "all_images"},
            "destination": {"kind": "local", "root_id": root_id},
        },
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["readiness"] == "ready"
    assert payload["counts"]["eligible_items"] == 1
    assert payload["counts"]["known_bytes_total"] == 12

    admitted = client.post(
        "/api/storage-migrations/admit",
        json={"preview_id": payload["id"], "expected_preview_version": payload["version"]},
    )
    assert admitted.status_code == 202
    migration = admitted.json()["detail"]
    assert migration["state"] == "queued"
    assert migration["counts"]["eligible_items"] == 1

    detail = client.get(f"/api/storage-migrations/{migration['id']}")
    assert detail.status_code == 200
    assert detail.json()["history"][0]["event_code"] == "admitted"

    canceled = client.post(
        f"/api/storage-migrations/{migration['id']}/actions",
        json={"action": "cancel", "expected_version": migration["version"]},
    )
    assert canceled.status_code == 200
    assert canceled.json()["detail"]["state"] == "canceled_with_remaining"
