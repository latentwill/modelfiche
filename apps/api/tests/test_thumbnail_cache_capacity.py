from __future__ import annotations

from pathlib import Path

from titles_api.storage.cache import ThumbnailCache
from PIL import Image

from titles_api.asset_cache import AssetCache
from titles_api.integrations.config import CacheSettings


def test_thumbnail_cache_evicts_unleased_lru_and_accounts_for_foreign_bytes(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "cache", quota_bytes=80)
    first = cache.write_complete("first", b"a" * 20)
    second = cache.write_complete("second", b"b" * 20)
    (cache.root / "foreign.bin").write_bytes(b"f" * 20)
    cache.acquire("first")

    grant = cache.reserve_thumbnail("third", source_bytes=8, temporary_bytes=8, output_bytes=16)

    assert first.exists()
    assert not second.exists()
    assert grant.total_bytes == 32
    assert cache.foreign_bytes() == 20
    cache.release_thumbnail(grant.grant_id)
    cache.release("first")


def test_thumbnail_cache_commits_reserved_output_without_double_counting(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "cache", quota_bytes=16)
    grant = cache.reserve_thumbnail("thumbnail", source_bytes=0, temporary_bytes=0, output_bytes=16)

    path = cache.commit_thumbnail(grant.grant_id, b"w" * 16)

    assert path.read_bytes() == b"w" * 16


def test_asset_cache_counts_completed_thumbnail_toward_shared_cache_quota(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 32), color=(90, 30, 180)).save(source)
    cache = AssetCache(CacheSettings(root=tmp_path / "cache", max_bytes=512 * 1024**2))
    thumbnail = cache.thumbnail("asset-1", source, 256)

    assert thumbnail.is_file()
    assert cache.used_bytes() >= thumbnail.stat().st_size


def test_asset_cache_retains_hydration_compatibility(tmp_path: Path) -> None:
    cache = AssetCache(CacheSettings(root=tmp_path / "cache", max_bytes=512 * 1024**2))

    entry = cache.hydrate(7, lambda destination: destination.write(b"content"))

    assert entry.path.read_bytes() == b"content"
    assert entry.size == 7


def test_asset_cache_persists_verified_hydration_outside_transient_cache(tmp_path: Path) -> None:
    cache = AssetCache(CacheSettings(root=tmp_path / "cache", max_bytes=512 * 1024**2))

    entry = cache.hydrate(7, lambda destination: destination.write(b"content"))
    durable = cache.persist(entry, tmp_path / "assets")
    entry.path.unlink()

    assert durable.path.read_bytes() == b"content"
    assert durable.path.is_relative_to(tmp_path / "assets")
