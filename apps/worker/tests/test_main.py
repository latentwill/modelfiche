from __future__ import annotations

import os

from titles_worker.main import configure_storage_environment


def test_worker_derives_storage_roots_from_local_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TITLES_LOCAL_ROOT", str(tmp_path))
    for name in (
        "TITLES_ASSET_ROOT",
        "TITLES_CACHE_ROOT",
        "TITLES_EXPORT_ROOT",
        "TITLES_CONFIG_ROOT",
        "TITLES_WANDB_UPLOAD_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    configure_storage_environment()

    assert os.environ["TITLES_ASSET_ROOT"] == str(tmp_path / "assets")
    assert os.environ["TITLES_CACHE_ROOT"] == str(tmp_path / "cache")
    assert os.environ["TITLES_EXPORT_ROOT"] == str(tmp_path / "exports")
    assert os.environ["TITLES_CONFIG_ROOT"] == str(tmp_path / "config")
    assert os.environ["TITLES_WANDB_UPLOAD_ROOT"] == str(tmp_path / "wandb-uploads")


def test_worker_preserves_explicit_storage_roots(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "explicit-cache"
    monkeypatch.setenv("TITLES_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("TITLES_CACHE_ROOT", str(explicit))

    configure_storage_environment()

    assert os.environ["TITLES_CACHE_ROOT"] == str(explicit)
