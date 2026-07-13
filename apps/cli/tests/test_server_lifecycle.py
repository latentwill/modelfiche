from __future__ import annotations

import os
from pathlib import Path

from titles_cli import main


def _write_pid(path: Path, pid: int | None = None) -> None:
    path.write_text(str(pid or os.getpid()), encoding="utf-8")


def test_stack_state_requires_every_service(tmp_path, monkeypatch) -> None:
    var = tmp_path / "var"
    var.mkdir()
    paths = main._service_pid_paths(var)
    for path in paths.values():
        _write_pid(path)
    (var / "server.ready").write_text("ready", encoding="utf-8")
    monkeypatch.setattr(main, "_url_is_ready", lambda _url: True)

    state = main._stack_state(tmp_path)

    assert state["ok"] is True
    assert state["ready"] is True
    assert {name for name, service in state["services"].items() if service["healthy"]} == {
        "supervisor",
        "api",
        "worker",
        "web",
    }

    paths["worker"].unlink()
    state = main._stack_state(tmp_path)

    assert state["ok"] is False
    assert state["services"]["worker"] == {"pid": None, "running": False, "healthy": False}


def test_stack_state_requires_readiness_marker(tmp_path, monkeypatch) -> None:
    var = tmp_path / "var"
    var.mkdir()
    for path in main._service_pid_paths(var).values():
        _write_pid(path)
    monkeypatch.setattr(main, "_url_is_ready", lambda _url: True)

    state = main._stack_state(tmp_path)

    assert state["ready"] is False
    assert state["ok"] is False
