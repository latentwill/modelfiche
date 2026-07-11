from uuid import uuid4

from fastapi.testclient import TestClient


def test_migration_preview_is_gated_before_admission(client: TestClient):
    response = client.post(
        "/api/storage-migrations/preview",
        json={
            "scope": {"kind": "all_images"},
            "destination": {"kind": "local", "root_id": str(uuid4())},
            "replacement_parent_id": None,
            "expected_parent_version": None,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "storage_feature_not_ready"
