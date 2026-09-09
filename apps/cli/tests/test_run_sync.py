from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from titles_cli import run_sync


class FakeResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeS3:
    class exceptions:
        ClientError = ClientError

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def list_objects_v2(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Contents": []}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
        return {
            "ContentLength": len(self.objects[Key]),
            "ETag": '"object-etag"',
            "Metadata": self.metadata.get(Key, {}),
        }

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, Any]) -> None:
        del bucket
        self.objects[key] = Path(filename).read_bytes()
        self.metadata[key] = dict(ExtraArgs.get("Metadata", {}))

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_kwargs: Any) -> dict[str, Any]:
        del Bucket
        self.objects[Key] = Body
        return {"ETag": '"manifest-etag"', "VersionId": "manifest-v1"}


def _manifest(output: Path) -> dict[str, Any]:
    return {
        "schema_version": "modelfiche.training-launch.v1",
        "run": {"id": "dam-run-1"},
        "backup": {
            "schema_version": "modelfiche.run-backup.v1",
            "source_directory": str(output),
            "credential_environment": {
                "access_key": "TRAINING_S3_ACCESS_KEY_ID",
                "secret_key": "TRAINING_S3_SECRET_ACCESS_KEY",
                "session_token": "TRAINING_S3_SESSION_TOKEN",
            },
            "destination": {
                "bucket": "training",
                "prefix": "projects/p1/runs/r1/",
                "endpoint_url": None,
                "region": "us-east-1",
                "addressing_style": "auto",
            },
            "checkpoint_handoff": {
                "manifest_prefix": "projects/p1/runs/r1/manifests/checkpoints/",
                "callback_url": "https://wandb.example/api/checkpoint-handoffs/dam-run-1",
                "callback_required": False,
            },
        },
    }


def test_refresh_sync_replaces_mutable_file_only_when_content_changes(tmp_path) -> None:
    log = tmp_path / "logs" / "training.log"
    log.parent.mkdir()
    log.write_bytes(b"first")
    old = time.time() - 120
    os.utime(log, (old, old))
    s3 = FakeS3()
    sync_sets = [{"local": "logs/", "remote": "logs/", "refresh": True}]

    first = run_sync._sync_files(
        s3,
        bucket="training",
        output=tmp_path,
        run_prefix="projects/p1/runs/r1/",
        sync_sets=sync_sets,
        stable_seconds=60,
    )
    log.write_bytes(b"final")
    os.utime(log, (old, old))
    second = run_sync._sync_files(
        s3,
        bucket="training",
        output=tmp_path,
        run_prefix="projects/p1/runs/r1/",
        sync_sets=sync_sets,
        stable_seconds=60,
    )
    unchanged = run_sync._sync_files(
        s3,
        bucket="training",
        output=tmp_path,
        run_prefix="projects/p1/runs/r1/",
        sync_sets=sync_sets,
        stable_seconds=60,
    )

    key = "projects/p1/runs/r1/logs/training.log"
    assert (first, second, unchanged) == (1, 1, 0)
    assert s3.objects[key] == b"final"


def test_sync_uploads_stable_checkpoint_then_publishes_manifest_and_callback(tmp_path, monkeypatch) -> None:
    checkpoint = tmp_path / "checkpoints" / "checkpoint-1000.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"stable checkpoint")
    old = time.time() - 120
    os.utime(checkpoint, (old, old))
    s3 = FakeS3()
    requests = []
    monkeypatch.setattr(run_sync, "_client", lambda *_args: s3)
    monkeypatch.setattr(run_sync.urllib.request, "urlopen", lambda request, timeout: requests.append((request, timeout)) or FakeResponse())
    monkeypatch.setenv("MODELFICHE_HANDOFF_TOKEN", "M" * 64)

    result = run_sync.sync_once(_manifest(tmp_path), stable_seconds=60)

    assert result["uploaded"] == 1
    assert result["generation"] == 1
    assert result["callback_status"] == "delivered"
    checkpoint_keys = [key for key in s3.objects if "/checkpoints/" in key and not key.endswith(".json")]
    assert len(checkpoint_keys) == 1
    published = json.loads(s3.objects[result["manifest_key"]])
    assert published["run_id"] == "dam-run-1"
    assert published["checkpoints"][0]["step"] == 1000
    assert published["checkpoints"][0]["object_key"] == checkpoint_keys[0]
    callback = json.loads(requests[0][0].data)
    assert callback["manifest_sha256"] == result["manifest_sha256"]
    assert callback["manifest_etag"] == "manifest-etag"
    assert requests[0][0].headers["Authorization"].startswith("Basic ")


def test_sync_succeeds_when_optional_callback_is_unavailable(tmp_path, monkeypatch) -> None:
    checkpoint = tmp_path / "checkpoints" / "checkpoint-1000.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"stable checkpoint")
    old = time.time() - 120
    os.utime(checkpoint, (old, old))
    s3 = FakeS3()
    monkeypatch.setattr(run_sync, "_client", lambda *_args: s3)
    monkeypatch.setenv("MODELFICHE_HANDOFF_TOKEN", "M" * 64)

    def unavailable(*_args, **_kwargs):
        raise run_sync.urllib.error.HTTPError("https://wandb.example", 403, "blocked", {}, None)

    monkeypatch.setattr(run_sync.urllib.request, "urlopen", unavailable)
    result = run_sync.sync_once(_manifest(tmp_path), stable_seconds=60)

    assert result["callback_status"] == "unavailable"
    assert result["manifest_key"] in s3.objects


def test_sync_does_not_publish_checkpoint_that_is_still_changing(tmp_path, monkeypatch) -> None:
    checkpoint = tmp_path / "checkpoints" / "checkpoint-10.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"still changing")
    s3 = FakeS3()
    monkeypatch.setattr(run_sync, "_client", lambda *_args: s3)

    result = run_sync.sync_once(_manifest(tmp_path), stable_seconds=60)

    assert result == {"uploaded": 0, "checkpoint_count": 0, "message": "no stable checkpoints are ready"}
    assert s3.objects == {}
