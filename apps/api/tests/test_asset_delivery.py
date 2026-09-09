from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import hashlib

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from titles_api import models
from titles_api.database import Base
from titles_api.settings import get_settings
from titles_api.storage.contracts import ContentVariantV1, ThumbnailVariantV1
from titles_api.storage.delivery import DeliveryTokenError, DeliveryTokenLedger
from titles_api.storage.gates import StorageGateStore
from titles_api.storage.repository import StorageRepository
from titles_api.routers.asset_delivery import AssetDeliveryService, DeliverySourceError, S3Payload, _Selected, _safe_local_path
from titles_api.storage.cache import ThumbnailCache, ThumbnailCapacityError
from titles_api.storage.thumbnails import ThumbnailRenderer
from titles_api.storage.thumbnails import ThumbnailLimits

def test_proxy_capability_is_bound_to_its_asset_revision_and_variant():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    asset_id = uuid4()
    revision_id = uuid4()
    ledger = DeliveryTokenLedger(clock=lambda: now)
    capability = ledger.issue_capability(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        location_id=uuid4(),
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
        expires_at=now + timedelta(minutes=5),
        payload="opaque reader",
    )

    with pytest.raises(DeliveryTokenError, match="revision"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=uuid4(),
            variant=ContentVariantV1(kind="content"),
            host="titles.local",
        )
    with pytest.raises(DeliveryTokenError, match="variant"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="titles.local",
        )

    assert ledger.resolve_capability(
        capability.token,
        asset_id=asset_id,
        asset_revision_id=revision_id,
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
    ).payload == "opaque reader"


def test_alternate_token_is_host_bound_short_lived_and_single_use():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    asset_id = uuid4()
    revision_id = uuid4()
    ledger = DeliveryTokenLedger(clock=lambda: now)
    alternate = ledger.issue_alternate(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        alternate_location_id=uuid4(),
        variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
        host="titles.local",
    )

    with pytest.raises(DeliveryTokenError, match="host"):
        ledger.consume_alternate(
            alternate.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="other.local",
        )

    assert ledger.consume_alternate(
        alternate.token,
        asset_id=asset_id,
        asset_revision_id=revision_id,
        variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
        host="titles.local",
    ).alternate_location_id == alternate.alternate_location_id
    with pytest.raises(DeliveryTokenError, match="unknown"):
        ledger.consume_alternate(
            alternate.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="titles.local",
        )


def test_expired_capability_is_not_resolved():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    ledger = DeliveryTokenLedger(clock=lambda: now)
    asset_id = uuid4()
    revision_id = uuid4()
    capability = ledger.issue_capability(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        location_id=uuid4(),
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
        expires_at=now + timedelta(seconds=1),
        payload=None,
    )

    now += timedelta(seconds=2)

    with pytest.raises(DeliveryTokenError, match="expired"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ContentVariantV1(kind="content"),
            host="titles.local",
        )


def test_remote_verified_location_without_content_blob_is_selectable():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        source = models.ImportSource(workspace_id=workspace.id, name="Training", bucket="training")
        asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.image,
            name="sample.png",
            mime_type="image/png",
        )
        session.add_all([workspace, project, source, asset])
        session.flush()
        location = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace.id,
            provider="s3",
            uri="s3://training/samples/sample.png",
            bucket="training",
            object_key="samples/sample.png",
            source_id=source.id,
            verified_sha256="a" * 64,
            verification_state="verified",
            hydration_state="remote",
        )
        session.add(location)
        session.flush()

        selected, alternate = StorageRepository(session, workspace.id).select_delivery_locations(asset.id)

        assert selected is location
        assert alternate is None


def test_local_delivery_rebases_cache_path_after_application_directory_moves(tmp_path: Path, monkeypatch):
    asset_root = tmp_path / "new-app" / "var" / "assets"
    cache_root = tmp_path / "new-app" / "var" / "cache"
    image_path = cache_root / "objects" / "ab" / "content"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(cache_root))
    get_settings.cache_clear()
    location = models.AssetLocation(
        provider="local",
        uri=str(tmp_path / "old-app" / "var" / "cache" / "objects" / "ab" / "content"),
    )

    assert _safe_local_path(location) == image_path.resolve()


def test_local_delivery_does_not_rebase_unrelated_outside_paths(tmp_path: Path, monkeypatch):
    asset_root = tmp_path / "app" / "var" / "assets"
    cache_root = tmp_path / "app" / "var" / "cache"
    asset_root.mkdir(parents=True)
    cache_root.mkdir(parents=True)
    outside = tmp_path / "other" / "content"
    outside.parent.mkdir()
    outside.write_bytes(b"secret")
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(cache_root))
    get_settings.cache_clear()
    location = models.AssetLocation(provider="local", uri=str(outside))

    with pytest.raises(DeliverySourceError, match="outside configured storage roots"):
        _safe_local_path(location)


def test_delivery_supports_legacy_verified_local_import_without_content_blob(tmp_path: Path, monkeypatch):
    asset_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    asset_root.mkdir()
    cache_root.mkdir()
    image_path = asset_root / "legacy.png"
    image_path.write_bytes(b"png")
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(cache_root))
    get_settings.cache_clear()

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind=models.AssetKind.image, name="legacy.png", mime_type="image/png")
        location = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace.id,
            provider="local",
            uri=str(image_path),
            size=3,
            verified_size=3,
            verified_sha256="a" * 64,
            verification_state="verified",
            hydration_state="hydrated",
        )
        session.add_all([workspace, project, asset, location])
        session.flush()

        result = AssetDeliveryService().descriptor(
            session,
            asset_revision_id=UUID(asset.id),
            variant=ContentVariantV1(kind="content"),
            host="testserver",
            base_url="http://testserver",
        )

        assert result["kind"] == "descriptor"
        assert result["descriptor"].provider == "local"
        assert result["descriptor"].selected_location_id == UUID(str(location.id))


def test_delivery_prefers_verified_local_copy_when_remote_gate_is_disabled(tmp_path: Path, monkeypatch):
    asset_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    asset_root.mkdir()
    cache_root.mkdir()
    image_path = cache_root / "image.jpg"
    image_path.write_bytes(b"jpg")
    monkeypatch.setenv("TITLES_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(cache_root))
    get_settings.cache_clear()

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="S3",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["images/"],
            addressing_style="auto",
            credential_env_prefix="S3",
        )
        asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.image,
            name="image.jpg",
            mime_type="image/jpeg",
        )
        session.add_all([workspace, project, source, asset])
        session.flush()
        repository = StorageRepository(session, workspace.id)
        local = repository.attach_verified_local_location(
            asset_id=asset.id,
            uri=str(image_path),
            size=3,
            sha256="a" * 64,
            mime_type="image/jpeg",
        )
        local.workspace_id = None
        remote = repository.attach_verified_remote_location(
            asset_id=asset.id,
            source_id=source.id,
            object_key="images/image.jpg",
            etag="etag",
            size=3,
            sha256="a" * 64,
            mime_type="image/jpeg",
        )
        asset.preferred_location_id = remote.id
        session.flush()
        raw = session.connection().connection.driver_connection
        StorageGateStore(raw).install_schema()
        result = AssetDeliveryService().descriptor(
            session,
            asset_revision_id=UUID(asset.id),
            variant=ContentVariantV1(kind="content"),
            host="testserver",
            base_url="http://testserver",
        )

        assert result["kind"] == "descriptor"
        assert result["descriptor"].provider == "local"

        session.delete(local)
        session.flush()
        remote_result = AssetDeliveryService(
            client_factory=lambda _source: object(),
        ).descriptor(
            session,
            asset_revision_id=UUID(asset.id),
            variant=ContentVariantV1(kind="content"),
            host="testserver",
            base_url="http://testserver",
        )
        assert remote_result["kind"] == "descriptor"
        assert remote_result["descriptor"].provider == "s3"
        assert result["descriptor"].selected_location_id == UUID(str(local.id))


def test_remote_thumbnail_uses_persistent_cache_before_opening_s3(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(cache_root))
    get_settings.cache_clear()
    image_bytes = BytesIO()
    Image.new("RGB", (32, 24), "navy").save(image_bytes, "PNG")
    content = image_bytes.getvalue()

    class Client:
        calls = 0

        def get_object(self, **_kwargs):
            self.calls += 1
            return {"Body": BytesIO(content), "ContentLength": len(content)}

    client = Client()
    asset = models.Asset(id=str(uuid4()), workspace_id=str(uuid4()), kind=models.AssetKind.image, name="remote.png", mime_type="image/png")
    location = models.AssetLocation(id=str(uuid4()), asset_id=asset.id, provider="s3", uri="s3://source/images/remote.png")
    selected = _Selected(asset, location, None)
    payload = S3Payload(client, "bucket", "images/remote.png", "source", "image/png", len(content), None, None, None, "spool")
    variant = ThumbnailVariantV1(kind="thumbnail", max_pixels=512)

    first = AssetDeliveryService()._thumbnail(payload, selected, variant)
    second = AssetDeliveryService()._thumbnail(payload, selected, variant)

    assert client.calls == 1
    assert first.path == second.path
    assert first.path.read_bytes().startswith(b"RIFF")
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("mode", "etag", "version_id", "expected_kwargs"),
    [
        ("conditional", "etag-1", None, {"IfMatch": "etag-1"}),
        ("versioned", None, "version-1", {"VersionId": "version-1"}),
        ("spool", "etag-1", "version-1", {"IfMatch": "etag-1", "VersionId": "version-1"}),
    ],
)
def test_remote_thumbnail_reads_the_bound_object_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    etag: str | None,
    version_id: str | None,
    expected_kwargs: dict[str, str],
):
    import titles_api.routers.asset_delivery as delivery_module

    image_bytes = BytesIO()
    Image.new("RGB", (32, 24), "navy").save(image_bytes, "PNG")
    content = image_bytes.getvalue()
    digest = hashlib.sha256(content).hexdigest()

    class Client:
        def __init__(self):
            self.calls: list[dict] = []

        def get_object(self, **kwargs):
            self.calls.append(kwargs)
            return {"Body": BytesIO(content), "ContentLength": len(content)}

    client = Client()
    cache = ThumbnailCache(tmp_path / "cache", quota_bytes=10_000_000)
    renderer = ThumbnailRenderer(
        cache,
        limits=ThumbnailLimits(max_source_bytes=1_000_000, max_output_bytes=1_000_000),
    )
    monkeypatch.setattr(delivery_module, "_shared_thumbnail_components", lambda: (cache, renderer))
    asset = models.Asset(
        id=str(uuid4()),
        workspace_id=str(uuid4()),
        kind=models.AssetKind.image,
        name="remote.png",
        mime_type="image/png",
    )
    location = models.AssetLocation(
        id=str(uuid4()),
        asset_id=asset.id,
        provider="s3",
        uri="s3://source/images/remote.png",
    )
    payload = S3Payload(
        client,
        "bucket",
        "images/remote.png",
        "source",
        "image/png",
        len(content),
        etag,
        version_id,
        digest,
        mode,
    )

    result = AssetDeliveryService()._thumbnail(
        payload,
        _Selected(asset, location, None),
        ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
    )

    assert result.path.is_file()
    assert client.calls == [
        {
            "Bucket": "bucket",
            "Key": "images/remote.png",
            **expected_kwargs,
        }
    ]
    with pytest.raises(ThumbnailCapacityError, match="unleased"):
        cache.reserve_thumbnail(
            "eviction-probe",
            source_bytes=0,
            temporary_bytes=9_000_000,
            output_bytes=1_000_000,
        )
    assert result.release is not None
    result.release()
    grant = cache.reserve_thumbnail(
        "eviction-probe",
        source_bytes=0,
        temporary_bytes=9_000_000,
        output_bytes=1_000_000,
    )
    cache.release_thumbnail(grant.grant_id)


class _FakeS3Body:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if self._data == b"":
            return b""
        chunk, self._data = self._data[:amount], self._data[amount:]
        return chunk

    def close(self) -> None:
        self.closed = True


class _FakeS3Client:
    def __init__(self, data: bytes):
        self.data = data
        self.calls: list[dict] = []

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return {"ContentLength": len(self.data), "ETag": '"etag"'}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        body = self.data
        range_header = kwargs.get("Range")
        if range_header:
            start_text, end_text = range_header.removeprefix("bytes=").split("-")
            start = int(start_text)
            end = int(end_text) if end_text else len(self.data) - 1
            body = self.data[start : end + 1]
        return {"Body": _FakeS3Body(body), "ContentLength": len(body)}


def _s3_service_with(data: bytes, *, size: int | None = None, sha: str | None = None):
    import hashlib
    from datetime import UTC, datetime, timedelta
    from titles_api.storage.contracts import ContentVariantV1
    from titles_api.routers.asset_delivery import DeliveryLimits

    now = datetime(2026, 7, 11, tzinfo=UTC)
    ledger = DeliveryTokenLedger(clock=lambda: now)
    client = _FakeS3Client(data)
    payload = S3Payload(
        client=client,
        bucket="bucket",
        key="key",
        source_id="source",
        mime_type="image/png",
        size=size if size is not None else len(data),
        etag=None,
        version_id=None,
        expected_sha256=sha,
        mode="conditional",
    )
    capability = ledger.issue_capability(
        asset_id=uuid4(),
        asset_revision_id=uuid4(),
        location_id=uuid4(),
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
        expires_at=now + timedelta(minutes=5),
        payload=payload,
    )
    service = AssetDeliveryService(ledger=ledger, limits=DeliveryLimits())
    return service, capability.token, client


def test_proxied_s3_content_buffers_and_sends_validators():
    from starlette.testclient import TestClient  # noqa: F401
    from fastapi.responses import StreamingResponse

    data = b"pngbytes" * 1000
    digest = hashlib.sha256(data).hexdigest()
    service, token, client = _s3_service_with(data, sha=digest)

    class _Request(dict):
        headers = {"if-none-match": None}

    response = service.serve(token, _Request(), host="titles.local")
    assert isinstance(response, StreamingResponse)
    assert response.headers["etag"] == f'"{digest}"'
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["content-length"] == str(len(data))
    import asyncio

    async def _collect():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(_collect()) == data
    # Buffered mode validates the whole object before the first byte.
    kinds = [call[0] for call in client.calls]
    assert kinds.count("head") >= 1


def test_if_none_match_returns_304_without_body():
    data = b"tiny"
    digest = hashlib.sha256(data).hexdigest()
    service, token, _client = _s3_service_with(data, sha=digest)

    class _Request:
        headers = {"if-none-match": f'"{digest}"'}

    response = service.serve(token, _Request(), host="titles.local")
    assert response.status_code == 304


def test_range_request_returns_206_slice():
    data = bytes(range(256))
    service, token, _client = _s3_service_with(data)

    class _Request:
        headers = {"range": "bytes=10-19"}

    response = service.serve(token, _Request(), host="titles.local")
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 10-19/{len(data)}"
    assert response.headers["content-length"] == "10"
    import asyncio

    async def _collect():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(_collect()) == data[10:20]


def test_unsatisfiable_range_returns_416():
    data = bytes(range(16))
    service, token, _client = _s3_service_with(data)

    class _Request:
        headers = {"range": "bytes=999-"}

    response = service.serve(token, _Request(), host="titles.local")
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(data)}"
