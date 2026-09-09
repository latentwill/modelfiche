import importlib

import pytest


def artifact_contract():
    return importlib.import_module("titles_api.storage.fal_artifacts")


def test_canonical_artifact_digest_ignores_rotated_urls_and_orders_ordinals():
    contract = artifact_contract()
    first = contract.canonicalize_artifacts(
        [
            {"ordinal": 1, "artifact_id": "provider-b", "url": "https://artifacts.example/b?old", "mime": "image/png", "width": 1024, "height": 1024, "seed": 2},
            {"ordinal": 0, "artifact_id": "provider-a", "url": "https://artifacts.example/a?old", "mime": "image/png", "width": 1024, "height": 1024, "seed": 1},
        ],
        expected_ordinals=(0, 1),
    )
    rotated = contract.canonicalize_artifacts(
        [
            {"ordinal": 0, "artifact_id": "provider-a", "url": "https://artifacts.example/a?new", "mime": "image/png", "width": 1024, "height": 1024, "seed": 1},
            {"ordinal": 1, "artifact_id": "provider-b", "url": "https://artifacts.example/b?new", "mime": "image/png", "width": 1024, "height": 1024, "seed": 2},
        ],
        expected_ordinals=(0, 1),
    )

    assert first.digest == rotated.digest
    assert [item.ordinal for item in first.items] == [0, 1]
    assert all("url" not in item.identity for item in first.items)


def test_canonical_artifact_list_rejects_missing_or_duplicate_admitted_ordinals():
    contract = artifact_contract()
    malformed = [{"ordinal": 0, "artifact_id": "provider-a", "url": "https://artifacts.example/a", "mime": "image/png", "width": 1024, "height": 1024, "seed": 1}]

    with pytest.raises(contract.ArtifactShapeError, match="expected ordinals"):
        contract.canonicalize_artifacts(malformed, expected_ordinals=(0, 1))

    with pytest.raises(contract.ArtifactShapeError, match="unique"):
        contract.canonicalize_artifacts(malformed * 2, expected_ordinals=(0, 1))
