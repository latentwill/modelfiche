from __future__ import annotations

import os
from pathlib import Path

import pytest

from titles_api.storage.local_root import LocalRoot, LocalRootSafetyError


def test_local_root_rejects_symlink_and_hard_link_targets(tmp_path: Path) -> None:
    root_path = tmp_path / "assets"
    root_path.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    os.symlink(outside, root_path / "linked")

    root = LocalRoot.open(root_path)
    try:
        with pytest.raises(LocalRootSafetyError, match="symlink"):
            root.read_bytes("linked/secret")

        (root_path / "owned").write_bytes(b"owned")
        os.chmod(root_path / "owned", 0o600)
        os.link(root_path / "owned", root_path / "linked-owned")
        with pytest.raises(LocalRootSafetyError, match="link count"):
            root.read_bytes("linked-owned")
    finally:
        root.close()


def test_local_root_fails_closed_when_configured_root_is_replaced(tmp_path: Path) -> None:
    root_path = tmp_path / "assets"
    root_path.mkdir(mode=0o700)
    root = LocalRoot.open(root_path)
    try:
        root_path.rename(tmp_path / "old-assets")
        root_path.mkdir(mode=0o700)

        with pytest.raises(LocalRootSafetyError, match="replaced"):
            root.assert_current()
    finally:
        root.close()


def test_local_root_requires_an_owner_only_directory(tmp_path: Path) -> None:
    root_path = tmp_path / "assets"
    root_path.mkdir()
    os.chmod(root_path, 0o755)

    with pytest.raises(LocalRootSafetyError, match="owner-only"):
        LocalRoot.open(root_path)
