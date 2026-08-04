from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO


POLL_INTERVAL = 0.25


def _root() -> Path:
    configured = os.environ.get("TITLES_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path.cwd().resolve()


def _http_ready(url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            return


def _write_pid(path: Path, process: subprocess.Popen[bytes]) -> None:
    path.write_text(str(process.pid), encoding="utf-8")


def _remove_pid_files(var: Path) -> None:
    for name in (
        "supervisor.pid",
        "server.pid",
        "wandb-ingress.pid",
        "worker.pid",
        "web.pid",
        "server.ready",
    ):
        (var / name).unlink(missing_ok=True)


def _wait_for_api(process: subprocess.Popen[bytes], url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("backend exited before becoming healthy")
        if _http_ready(url):
            return
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"backend did not become healthy at {url}")


def _wait_for_web(
    processes: dict[str, subprocess.Popen[bytes]],
    url: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in processes.items():
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited during startup")
        if _http_ready(url):
            return
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"frontend did not become ready at {url}")


def main() -> None:
    root = _root()
    var = root / "var"
    var.mkdir(parents=True, exist_ok=True)
    api_port = int(os.environ.get("TITLES_API_PORT", "8400"))
    wandb_ingress_port = int(os.environ.get("TITLES_WANDB_INGRESS_PORT", "8401"))
    web_port = int(os.environ.get("TITLES_WEB_PORT", "5173"))
    timeout = float(os.environ.get("TITLES_SERVER_START_TIMEOUT", "20"))
    pythonpath = os.pathsep.join(
        str(root / "apps" / name) for name in ("api", "worker", "cli")
    )
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "TITLES_PROJECT_ROOT": str(root),
    }
    logs: list[IO[bytes]] = []
    processes: dict[str, subprocess.Popen[bytes]] = {}
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        api_log = (var / "server.log").open("ab")
        logs.append(api_log)
        processes["api"] = subprocess.Popen(
            [sys.executable, "-m", "titles_api"],
            cwd=root,
            env=env,
            stdout=api_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _write_pid(var / "server.pid", processes["api"])
        _wait_for_api(
            processes["api"], f"http://127.0.0.1:{api_port}/api/health", timeout
        )
        wandb_ingress_log = (var / "wandb-ingress.log").open("ab")
        logs.append(wandb_ingress_log)
        processes["wandb"] = subprocess.Popen(
            [sys.executable, "-m", "titles_api.wandb_ingress_main"],
            cwd=root,
            env=env,
            stdout=wandb_ingress_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _write_pid(var / "wandb-ingress.pid", processes["wandb"])
        _wait_for_api(
            processes["wandb"],
            f"http://127.0.0.1:{wandb_ingress_port}/health",
            timeout,
        )

        worker_log = (var / "worker.log").open("ab")
        logs.append(worker_log)
        processes["worker"] = subprocess.Popen(
            [sys.executable, "-m", "titles_worker.main"],
            cwd=root,
            env=env,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _write_pid(var / "worker.pid", processes["worker"])

        web_log = (var / "web.log").open("ab")
        logs.append(web_log)
        processes["web"] = subprocess.Popen(
            [
                "pnpm",
                "--dir",
                "apps/web",
                "dev",
                "--host",
                "127.0.0.1",
                "--port",
                str(web_port),
            ],
            cwd=root,
            env=env,
            stdout=web_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _write_pid(var / "web.pid", processes["web"])
        _wait_for_web(processes, f"http://127.0.0.1:{web_port}/", timeout)
        (var / "server.ready").write_text(str(time.time()), encoding="utf-8")

        while not stopping:
            for name, process in processes.items():
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{name} exited while the service stack was running"
                    )
            time.sleep(POLL_INTERVAL)
    except Exception as exc:
        (var / "server.error").write_text(str(exc), encoding="utf-8")
        raise SystemExit(1) from exc
    finally:
        for process in reversed(list(processes.values())):
            _terminate(process)
        for log in logs:
            log.close()
        _remove_pid_files(var)
        (var / "server.error").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
