from fastapi.testclient import TestClient


def test_storage_policy_defaults_to_automatic_local_without_candidates(client: TestClient):
    response = client.get("/api/storage-policy")

    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "desired_provider": "local",
        "resolved_provider": "local",
        "selection_origin": "automatic",
        "write_source_id": None,
        "fal_local_fallback": True,
        "blocked_reason": None,
        "candidates": [],
    }
