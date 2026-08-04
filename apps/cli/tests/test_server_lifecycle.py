from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner
from titles_cli import main


runner = CliRunner()


def test_root_lifecycle_aliases_delegate_to_server_commands(monkeypatch) -> None:
    calls: list[str] = []

    def record(command: str):
        def handler(_ctx) -> None:
            calls.append(command)

        return handler

    monkeypatch.setattr(main, "server_start", record("start"))
    monkeypatch.setattr(main, "server_status", record("status"))
    monkeypatch.setattr(main, "server_stop", record("stop"))

    for command in ("start", "status", "stop"):
        result = runner.invoke(main.app, [command])

        assert result.exit_code == 0, result.output

    assert calls == ["start", "status", "stop"]


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
    assert {
        name for name, service in state["services"].items() if service["healthy"]
    } == {
        "supervisor",
        "api",
        "wandb",
        "worker",
        "web",
    }

    paths["worker"].unlink()
    state = main._stack_state(tmp_path)

    assert state["ok"] is False
    assert state["services"]["worker"] == {
        "pid": None,
        "running": False,
        "healthy": False,
    }


def test_stack_state_requires_readiness_marker(tmp_path, monkeypatch) -> None:
    var = tmp_path / "var"
    var.mkdir()
    for path in main._service_pid_paths(var).values():
        _write_pid(path)
    monkeypatch.setattr(main, "_url_is_ready", lambda _url: True)

    state = main._stack_state(tmp_path)

    assert state["ready"] is False
    assert state["ok"] is False
