"""Service lifecycle for the self-contained Windows app and its embedded CLI."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from titles_api.platform_io import process_alive
from titles_api.storage.run_lease import RunLease, RunLeaseBusy
from .server_supervisor import _http_ready, _terminate


def configure_bundle() -> Path:
    bundle = Path(os.environ.get("MODELFICHE_BUNDLE_ROOT", Path(sys.executable).parent.parent)).resolve()
    support = Path(os.environ.get("TITLES_APP_SUPPORT", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Modelfiche")).resolve()
    support.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("TITLES_API_PORT", "8400"))
    ingress = int(os.environ.get("TITLES_WANDB_INGRESS_PORT", "8401"))
    if not 1 <= port <= 65535 or not 1 <= ingress <= 65535 or port == ingress:
        raise ValueError("API and W&B ports must be distinct numbers between 1 and 65535")
    os.environ.update({
        "MODELFICHE_BUNDLE_ROOT": str(bundle),
        "TITLES_APP_SUPPORT": str(support),
        "TITLES_LOCAL_ROOT": str(support),
        "TITLES_DATABASE_URL": "sqlite:///" + (support / "modelfiche.sqlite3").as_posix(),
        "TITLES_WEB_DIST": str(bundle / "web"),
        "TITLES_ALEMBIC_SCRIPT_LOCATION": str(bundle / "alembic"),
        "TITLES_CLI_PATH": str(bundle / "mfiche.exe"),
        "TITLES_RUN_LOCK": str(support / "runtime.lock"),
        "TITLES_API_PORT": str(port),
        "TITLES_WANDB_INGRESS_PORT": str(ingress),
        "TITLES_WANDB_INGRESS_HOST": "127.0.0.1",
        "TITLES_WANDB_INGRESS_BASE_URL": f"http://127.0.0.1:{ingress}",
        "TITLES_CLI_ORIGIN": f"http://127.0.0.1:{port}",
        "TITLES_CORS_ORIGINS": json.dumps([f"http://127.0.0.1:{port}", f"http://localhost:{port}"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    })
    for key, child in {"ASSET_ROOT": "assets", "CACHE_ROOT": "cache", "EXPORT_ROOT": "exports", "CONFIG_ROOT": "config", "WANDB_UPLOAD_ROOT": "wandb-uploads"}.items():
        path = support / child
        path.mkdir(exist_ok=True)
        os.environ["TITLES_" + key] = str(path)
    (support / "logs").mkdir(exist_ok=True)
    return support


def status_bundle() -> dict:
    support = configure_bundle()
    try:
        state = json.loads((support / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "running": False, "ready": False, "services": {}}
    services = state["services"]
    for name, service in services.items():
        service["running"] = process_alive(service["pid"])
        service["healthy"] = service["running"] and (not service.get("url") or _http_ready(service["url"]))
    state["running"] = any(service["running"] for service in services.values())
    state["ok"] = state["ready"] and all(service["healthy"] for service in services.values())
    return state


def start_bundle() -> dict:
    if status_bundle()["ok"]:
        return status_bundle()
    subprocess.Popen([str(Path(os.environ["MODELFICHE_BUNDLE_ROOT"]) / "Modelfiche.exe")])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = status_bundle()
        if state["ok"]:
            return state
        time.sleep(0.25)
    return {"ok": False, "error": "Startup failed. Open Modelfiche or check its logs."}


def stop_bundle() -> dict:
    support = configure_bundle()
    if not status_bundle()["running"]:
        return {"ok": True, "running": False}
    (support / "desktop.stop").write_text("stop", encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not status_bundle()["running"]:
            return {"ok": True, "running": False}
        time.sleep(0.25)
    return {"ok": False, "error": "Services are still stopping. Check the launcher and logs."}


def serve() -> None:
    # The native launcher attaches us to its kill-on-close Job Object before
    # granting permission to spawn services. This removes the orphan-process race.
    if "--handshake" in sys.argv and sys.stdin.readline().strip() != "start":
        raise SystemExit(1)
    support = configure_bundle()
    os.chdir(support)
    ready_path = support / "desktop.json"
    stop_path = support / "desktop.stop"
    port = int(os.environ["TITLES_API_PORT"])
    ingress = int(os.environ["TITLES_WANDB_INGRESS_PORT"])
    origin = f"http://127.0.0.1:{port}"
    try:
        lease = RunLease.acquire(support / "desktop.lock")
    except RunLeaseBusy:
        for _ in range(120):
            state = status_bundle()
            if state["ok"]:
                print("ALREADY " + state["web_url"], flush=True)
                return
            time.sleep(0.5)
        raise RuntimeError("Another Modelfiche launcher is still starting. Check its window and logs.")
    processes = {}
    logs = []
    stopping = threading.Event()
    if "--handshake" in sys.argv:
        def read_stop():
            # EOF also stops the stack when its native owner goes away.
            sys.stdin.readline()
            stopping.set()
        threading.Thread(target=read_stop, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        ready_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)
        # Refuse occupied ports before migrations; never open an unrelated app.
        for number in (port, ingress):
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", number))
        modules = {"api": "titles_api", "worker": "titles_worker.main", "wandb": "titles_api.wandb_ingress_main"}
        urls = {"api": origin + "/api/health", "wandb": f"http://127.0.0.1:{ingress}/health"}
        for name, module in modules.items():
            log = (support / "logs" / (name + ".log")).open("ab")
            logs.append(log)
            processes[name] = subprocess.Popen([sys.executable, "-m", module], cwd=support, stdout=log, stderr=subprocess.STDOUT,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 120
            while name in urls and not _http_ready(urls[name]):
                if stopping.is_set() or stop_path.exists():
                    return
                if any(p.poll() is not None for p in processes.values()) or time.monotonic() > deadline:
                    raise RuntimeError(f"{name} could not start. See {support / 'logs' / (name + '.log')}")
                time.sleep(0.2)
        state = {"ok": True, "running": True, "ready": True, "web_url": origin + "/", "api_url": origin,
                 "services": {name: {"pid": p.pid, "url": urls.get(name)} for name, p in processes.items()}}
        state["services"]["supervisor"] = {"pid": os.getpid()}
        temporary = ready_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(ready_path)
        print("READY " + origin + "/", flush=True)
        while not stopping.wait(0.25) and not stop_path.exists():
            for name, process in processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} stopped unexpectedly. See {support / 'logs' / (name + '.log')}")
    finally:
        for process in reversed(list(processes.values())):
            _terminate(process)
            process.wait(timeout=10)
        for log in logs:
            log.close()
        ready_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)
        lease.release()


if __name__ == "__main__":
    serve()
