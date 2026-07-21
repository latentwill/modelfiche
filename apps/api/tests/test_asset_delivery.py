from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

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
