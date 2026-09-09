from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from titles_api import models
from titles_api.database import Base
from titles_api.storage import single_copy
from titles_api.storage.single_copy import audit_single_copy, ensure_remote, evict_local


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.puts = 0
        self.multipart: dict[str, dict[int, bytes]] = {}
        self.multipart_metadata: dict[str, dict[str, str]] = {}
        self.multipart_key: dict[str, str] = {}
        self.multipart_counter = 0

    def create_multipart_upload(self, *, Bucket: str, Key: str, Metadata: dict[str, str]) -> dict[str, str]:
        del Bucket
        self.multipart_counter += 1
        upload_id = str(self.multipart_counter)
        self.multipart[upload_id] = {}
        self.multipart_metadata[upload_id] = Metadata
        self.multipart_key[upload_id] = Key
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes) -> dict[str, str]:
        del Bucket, Key
        self.multipart[UploadId][PartNumber] = Body
        return {"ETag": f"part-{PartNumber}"}

    def list_parts(self, *, Bucket: str, Key: str, UploadId: str, **kwargs) -> dict[str, object]:
        del Bucket, Key, kwargs
        return {
            "Parts": [
                {"PartNumber": number, "ETag": f"part-{number}"}
                for number in sorted(self.multipart[UploadId])
            ]
        }

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, object],
    ) -> None:
        del Bucket, MultipartUpload
        body = b"".join(self.multipart[UploadId][number] for number in sorted(self.multipart[UploadId]))
        self.objects[Key] = {"body": body, "metadata": self.multipart_metadata[UploadId]}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        del Bucket, Key
        self.multipart.pop(UploadId, None)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        if Key not in self.objects:
            raise KeyError(Key)
        value = self.objects[Key]
        return {
            "ContentLength": len(value["body"]),
            "Metadata": value["metadata"],
            "ETag": '"etag"',
        }

    def put_object(self, *, Bucket: str, Key: str, Body, Metadata: dict[str, str]) -> None:
        del Bucket
        self.puts += 1
        self.objects[Key] = {"body": Body.read(), "metadata": Metadata}


def _setup(tmp_path: Path, *, content: bytes = b"original", digest: str | None = None):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    root = tmp_path / "assets"
    root.mkdir()
    path = root / "original.png"
    path.write_bytes(content)
    digest = digest or sha256(content).hexdigest()
    with Session(engine) as db:
        workspace = models.Workspace(name="workspace")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="S4",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["titles/"],
            managed_prefix="titles/managed/",
        )
        asset = models.Asset(
            workspace_id=workspace.id,
            name="original.png",
            mime_type="image/png",
            sha256=digest,
        )
        local = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace.id,
            provider="local",
            uri=str(path),
            size=len(content),
            verified_size=len(content),
            verified_sha256=digest,
            verification_state="verified",
            hydration_state="hydrated",
        )
        db.add_all([workspace, source, asset, local])
        db.flush()
        yield db, workspace, source, asset, local, root


def _factory(client: FakeS3):
    return lambda settings: client


def test_dry_run_does_not_upload_or_register(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        client = FakeS3()
        before = len(db.scalars(select(models.AssetLocation)).all())
        report = ensure_remote(
            db,
            workspace.id,
            source.id,
            [root],
            dry_run=True,
            client_factory=_factory(client),
        )
        assert report.uploaded == 0
        assert report.would_upload == 1
        assert report.bytes_would_upload == len(b"original")
        assert report.skipped == 0
        assert client.puts == 0
        assert len(db.scalars(select(models.AssetLocation)).all()) == before
        assert asset.preferred_location_id is None


def test_existing_verified_object_is_reused_idempotently(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        client = FakeS3()
        digest = asset.sha256
        key = f"titles/managed/library/{digest[:2]}/{digest}.png"
        client.objects[key] = {"body": b"original", "metadata": {"sha256": digest}}
        first = ensure_remote(db, workspace.id, source.id, [root], client_factory=_factory(client))
        second = ensure_remote(db, workspace.id, source.id, [root], client_factory=_factory(client))
        assert first.reused == second.reused == 1
        assert first.uploaded == second.uploaded == 0
        assert client.puts == 0
        assert len(db.scalars(select(models.AssetLocation).where(models.AssetLocation.provider == "s3")).all()) == 1


def test_upload_verifies_and_registers_deterministic_location(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        client = FakeS3()
        report = ensure_remote(db, workspace.id, source.id, [root], client_factory=_factory(client))
        assert report.uploaded == 1
        assert report.bytes_uploaded == len(b"original")
        key = next(iter(client.objects))
        assert key == f"titles/managed/library/{asset.sha256[:2]}/{asset.sha256}.png"
        location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.provider == "s3"))
        assert location is not None
        assert location.object_key == key
        assert location.verification_state == "available"
        assert location.verified_sha256 == asset.sha256



def test_shared_digest_registers_one_object_for_each_asset(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        sibling = models.Asset(
            workspace_id=workspace.id,
            name="duplicate.png",
            mime_type="image/png",
            sha256=asset.sha256,
        )
        sibling_local = models.AssetLocation(
            asset_id=sibling.id,
            workspace_id=workspace.id,
            provider="local",
            uri=local.uri,
            size=local.size,
            verified_size=local.verified_size,
            verified_sha256=local.verified_sha256,
            verification_state="verified",
            hydration_state="hydrated",
        )
        db.add_all([sibling, sibling_local])
        db.flush()
        client = FakeS3()

        report = ensure_remote(
            db,
            workspace.id,
            source.id,
            [root],
            client_factory=_factory(client),
        )

        locations = db.scalars(
            select(models.AssetLocation).where(models.AssetLocation.provider == "s3")
        ).all()
        assert report.failed == 0
        assert report.uploaded == 1
        assert report.reused == 1
        assert client.puts == 1
        assert {location.asset_id for location in locations} == {asset.id, sibling.id}
        assert len({location.object_key for location in locations}) == 1


def test_mismatched_local_checksum_is_refused(tmp_path: Path):
    wrong = "0" * 64
    for db, workspace, source, asset, local, root in _setup(tmp_path, digest=wrong):
        client = FakeS3()
        report = ensure_remote(db, workspace.id, source.id, [root], client_factory=_factory(client))
        assert report.failed == 1
        assert report.failures == [{"asset_id": asset.id, "code": "checksum_mismatch"}]
        assert client.puts == 0
        assert db.scalar(select(models.AssetLocation).where(models.AssetLocation.provider == "s3")) is None


def test_root_escape_is_refused_without_unlink(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        local.uri = str(outside)
        local.verified_size = len(b"outside")
        remote = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace.id,
            provider="s3",
            uri="s3://source/key",
            bucket=source.bucket,
            object_key="key",
            source_id=source.id,
            verified_size=len(b"outside"),
            verified_sha256=asset.sha256,
            verification_state="verified",
        )
        db.add(remote)
        db.flush()
        report = evict_local(db, workspace.id, [root])
        assert report.failed == 1
        assert report.failures[0]["code"] == "outside_root"
        assert outside.exists()
        assert db.get(models.AssetLocation, local.id) is not None


def test_missing_local_file_retires_stale_row(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        Path(local.uri).unlink()
        report = evict_local(db, workspace.id, [root])
        assert report.stale_pruned == 1
        assert local.verification_state == "missing"
        assert local.hydration_state == "remote_missing"


def test_eviction_repoints_preferred_and_origin_to_verified_remote(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        remote = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace.id,
            provider="s3",
            uri="s3://source/key",
            bucket=source.bucket,
            object_key="key",
            source_id=source.id,
            verified_size=len(b"original"),
            verified_sha256=asset.sha256,
            verification_state="verified",
        )
        db.add(remote)
        db.flush()
        asset.preferred_location_id = local.id
        asset.origin_location_id = local.id
        db.flush()
        report = evict_local(db, workspace.id, [root])
        assert report.evicted == 1
        assert report.bytes_evicted == len(b"original")
        assert not Path(local.uri).exists()
        assert asset.preferred_location_id == remote.id
        assert asset.origin_location_id == remote.id
        assert local.verification_state == "missing"
        assert local.hydration_state == "remote_missing"


def test_audit_is_read_only(tmp_path: Path):
    for db, workspace, source, asset, local, root in _setup(tmp_path):
        report = audit_single_copy(db, workspace.id, source.id, [root])
        assert report.examined == 1
        assert report.already_remote == 0
        assert report.uploaded == report.evicted == report.stale_pruned == 0


def test_large_file_uses_sequential_multipart_upload(tmp_path: Path, monkeypatch):
    content = b"0123456789"
    for db, workspace, source, asset, local, root in _setup(tmp_path, content=content):
        monkeypatch.setattr(single_copy, "_MULTIPART_THRESHOLD", 4)
        monkeypatch.setattr(single_copy, "_MULTIPART_PART_SIZE", 4)
        client = FakeS3()
        report = ensure_remote(db, workspace.id, source.id, [root], client_factory=_factory(client))
        assert report.uploaded == 1
        assert client.puts == 0
        assert client.multipart_counter == 1
        key = next(iter(client.objects))
        assert client.objects[key]["body"] == content
