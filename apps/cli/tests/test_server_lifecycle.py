from __future__ import annotations

import os
import pytest
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


def test_start_reexecs_through_1password_when_runtime_references_exist(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env.1password").write_text(
        "TITLES_REMOTE_ACCESS_ORIGIN=https://modelfiche.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TITLES_REMOTE_ACCESS_ORIGIN", raising=False)
    monkeypatch.delenv("MODELFICHE_RUNTIME_ENV_LOADED", raising=False)
    monkeypatch.setattr(main.shutil, "which", lambda command: "/usr/local/bin/op" if command == "op" else None)
    monkeypatch.setattr(main.sys, "argv", ["mfiche", "start"])
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(main.os, "execvpe", lambda executable, args, env: calls.append((executable, args, env)))

    main._load_runtime_environment(tmp_path)

    assert len(calls) == 1
    executable, args, env = calls[0]
    assert executable == "/usr/local/bin/op"
    assert args == [
        "/usr/local/bin/op",
        "run",
        f"--env-file={tmp_path / '.env.1password'}",
        "--",
        main.sys.executable,
        "-m",
        "titles_cli.main",
        "start",
    ]
    assert env["MODELFICHE_RUNTIME_ENV_LOADED"] == "1"
    assert env["TITLES_PROJECT_ROOT"] == str(tmp_path)


def test_start_uses_existing_runtime_environment(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env.1password").write_text(
        "TITLES_REMOTE_ACCESS_ORIGIN=https://modelfiche.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_ORIGIN", "https://modelfiche.example.com")
    monkeypatch.setattr(main.os, "execvpe", lambda *_args: pytest.fail("must not re-exec"))
    main._load_runtime_environment(tmp_path)



def test_stop_terminates_every_service(tmp_path, monkeypatch) -> None:
    var = tmp_path / "var"
    var.mkdir()
    for path in main._service_pid_paths(var).values():
        _write_pid(path, 100)
    stopped: list[int] = []
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "_pid_is_alive", lambda _path: (100, True))
    monkeypatch.setattr(main, "_terminate_pid", stopped.append)

    result = runner.invoke(main.app, ["stop"])

    assert result.exit_code == 0, result.output
    assert stopped == [100, 100, 100, 100, 100]
    assert not any(path.exists() for path in main._service_pid_paths(var).values())


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


def test_project_root_discovers_source_checkout_without_private_plans(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'titles-dam'\n")
    package = tmp_path / "apps" / "cli" / "titles_cli"
    package.mkdir(parents=True)
    monkeypatch.delenv("TITLES_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(package)
    assert main.project_root() == tmp_path
