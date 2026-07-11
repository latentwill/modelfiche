from __future__ import annotations

import hashlib
import io
from pathlib import Path
from uuid import uuid4

from PIL import Image
import pytest

from titles_api.storage.cache import ThumbnailCache
from titles_api.storage.thumbnails import ThumbnailError, ThumbnailLimits, ThumbnailRenderer


def _png(width: int, height: int) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), color=(20, 40, 60)).save(stream, "PNG")
    return stream.getvalue()


def _renderer(tmp_path: Path, *, limits: ThumbnailLimits) -> ThumbnailRenderer:
    cache = ThumbnailCache(tmp_path / "cache", quota_bytes=10_000_000)
    return ThumbnailRenderer(cache, limits=limits)


def test_thumbnail_renderer_writes_revision_keyed_webp_after_hash_verification(tmp_path: Path):
    source = _png(80, 40)
    renderer = _renderer(tmp_path, limits=ThumbnailLimits(max_source_bytes=1_000_000, max_output_bytes=1_000_000))

    result = renderer.render(
        asset_revision_id=uuid4(),
        max_pixels=256,
        source=io.BytesIO(source),
        source_size=len(source),
        expected_sha256=hashlib.sha256(source).hexdigest(),
    )

    assert result.path.suffix == ".webp"
    with Image.open(result.path) as image:
        assert image.format == "WEBP"
        assert image.width <= 256
        assert image.height <= 256


def test_thumbnail_renderer_rejects_hash_drift_and_releases_its_grant(tmp_path: Path):
    source = _png(12, 12)
    renderer = _renderer(tmp_path, limits=ThumbnailLimits(max_source_bytes=1_000_000, max_output_bytes=1_000_000))

    with pytest.raises(ThumbnailError, match="checksum"):
        renderer.render(
            asset_revision_id=uuid4(),
            max_pixels=256,
            source=io.BytesIO(source),
            source_size=len(source),
            expected_sha256="0" * 64,
        )

    result = renderer.render(
        asset_revision_id=uuid4(),
        max_pixels=256,
        source=io.BytesIO(source),
        source_size=len(source),
        expected_sha256=hashlib.sha256(source).hexdigest(),
    )
    assert result.path.is_file()


def test_thumbnail_renderer_rejects_oversized_or_over_pixel_sources_before_publication(tmp_path: Path):
    renderer = _renderer(tmp_path, limits=ThumbnailLimits(max_source_bytes=64, max_output_bytes=64, max_pixels=100))

    with pytest.raises(ThumbnailError, match="source"):
        renderer.render(
            asset_revision_id=uuid4(),
            max_pixels=256,
            source=io.BytesIO(b"x" * 65),
            source_size=None,
            expected_sha256=None,
        )

    renderer = _renderer(
        tmp_path,
        limits=ThumbnailLimits(max_source_bytes=1_000_000, max_output_bytes=1_000_000, max_pixels=100),
    )
    source = _png(11, 10)
    with pytest.raises(ThumbnailError, match="pixel"):
        renderer.render(
            asset_revision_id=uuid4(),
            max_pixels=256,
            source=io.BytesIO(source),
            source_size=len(source),
            expected_sha256=hashlib.sha256(source).hexdigest(),
        )
