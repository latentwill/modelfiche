from __future__ import annotations

import json
import os
import pytest
from titles_cli.desktop import configure_bundle, status_bundle


@pytest.fixture
def bundle_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("MODELFICHE_BUNDLE_ROOT", str(tmp_path / "Application"))
    monkeypatch.setenv("TITLES_APP_SUPPORT", str(tmp_path / "Data café"))
    monkeypatch.setenv("TITLES_API_PORT", "19400")
    monkeypatch.setenv("TITLES_WANDB_INGRESS_PORT", "19401")
    return tmp_path


def test_all_packaged_state_lives_in_user_data(bundle_environment):
    root = configure_bundle()
    assert "Data café" in str(root)
    assert os.environ["TITLES_DATABASE_URL"].endswith((root / "modelfiche.sqlite3").as_posix())
    for name in ("ASSET_ROOT", "CACHE_ROOT", "CONFIG_ROOT", "EXPORT_ROOT", "WANDB_UPLOAD_ROOT"):
        assert os.environ["TITLES_" + name].startswith(str(root))
    assert json.loads(os.environ["TITLES_CORS_ORIGINS"])[0] == "http://127.0.0.1:19400"
    from titles_cli.client import LocalContext
    assert LocalContext.load().api_url == "http://127.0.0.1:19400"
    assert status_bundle() == {"ok": False, "running": False, "ready": False, "services": {}}


def test_bad_ports_fail_before_start(bundle_environment, monkeypatch):
    monkeypatch.setenv("TITLES_API_PORT", "19401")
    with pytest.raises(ValueError, match="distinct"):
        configure_bundle()
