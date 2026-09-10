"""Exercise the exact ZIP through its native launchers on a disposable Windows profile."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
VALIDATION = ROOT / "dist/windows/validation"
VALIDATION.mkdir(parents=True, exist_ok=True)
EXTRACTED = VALIDATION / "Extracted app café with spaces"
EXTRACTED.mkdir(exist_ok=True)
archive = next((ROOT / "dist/windows/release").glob("*.zip"))
with zipfile.ZipFile(archive) as zipped:
    zipped.extractall(EXTRACTED)
bundle = EXTRACTED / "Modelfiche"
support = VALIDATION / "Workspace café with spaces"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


port, ingress = free_port(), free_port()
while ingress == port:
    ingress = free_port()
origin = f"http://127.0.0.1:{port}"
env = {**os.environ, "TITLES_APP_SUPPORT": str(support), "TITLES_API_PORT": str(port),
       "TITLES_WANDB_INGRESS_PORT": str(ingress), "MODELFICHE_LAUNCHER_SCREENSHOT": str(VALIDATION / "native-launcher.png")}
for key in ("PYTHONPATH", "PYTHONHOME", "MODELFICHE_BUNDLE_ROOT", "MODELFICHE_STARTUP_DIAGNOSTICS"):
    env.pop(key, None)
headers = {"Origin": origin}
checks = []
process = None


def record(message):
    checks.append(message)
    print(message, flush=True)


def request(path, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(origin + path, data=body, method=method, headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw


def cli(*args, ok=True):
    result = subprocess.run([str(bundle / "mfiche.exe"), "--json", *args], cwd=EXTRACTED, env=env, capture_output=True, text=True, encoding="utf-8", timeout=150)
    assert ok is None or (result.returncode == 0) == ok, (args, result.stdout, result.stderr)
    return json.loads(result.stdout)


def launch():
    p = subprocess.Popen([str(bundle / "Modelfiche.exe"), "--no-browser"], cwd=EXTRACTED, env=env)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        assert p.poll() is None, "Native launcher exited during startup"
        try:
            state = json.loads((support / "desktop.json").read_text(encoding="utf-8"))
            if state["ready"]:
                request("/api/health")
                return p, state
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    p.kill()
    p.wait(timeout=10)
    raise AssertionError("Native launcher did not become ready; see validation logs")


def close_window(pid):
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    found = []
    @callback_type
    def callback(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid:
            user.PostMessageW(window, 0x0010, 0, 0)
            found.append(window)
        return True
    user.EnumWindows(callback, 0)
    assert found, "Launcher window was not found"


try:
    support.mkdir(exist_ok=True)
    probe_code = "import sys,subprocess; print(sys.executable, flush=True); r=subprocess.run([sys.executable, '-u', '-c', 'print(123)'], capture_output=True, timeout=15); print(r.returncode, r.stdout, r.stderr, flush=True); import titles_api.app; print('API import OK', flush=True)"
    with (VALIDATION / "runtime-probe.log").open("wb") as probe_log:
        probe = subprocess.run([str(bundle / "runtime/python.exe"), "-u", "-X", "faulthandler", "-c", probe_code], cwd=support, env=env, stdin=subprocess.DEVNULL, stdout=probe_log, stderr=subprocess.STDOUT, timeout=60)
    assert probe.returncode == 0, "Embedded runtime probe failed; see runtime-probe.log"
    record("Embedded Python, nested subprocess, and full API import")
    process, state = launch()
    assert b'<html' in request("/").lower()
    assert len(state["services"]) == 4
    assert cli("status")["ok"]
    assert cli("doctor")["database"]["ok"]
    assert cli("capabilities")
    record("Extracted native launcher, bundled services, static UI, doctor, capabilities, status")
    workspaces = cli("workspace", "list")
    workspace = workspaces[0]["id"]
    headers["X-Workspace-Id"] = workspace
    project = request("/api/projects", {"title": "Windows smoke café"})
    assert project["title"] == "Windows smoke café"
    picture = io.BytesIO()
    Image.new("RGB", (96, 64), (90, 120, 150)).save(picture, format="PNG")
    payload = picture.getvalue()
    imported = request("/api/local-imports", {"project_id": project["id"], "dataset_name": "Windows images", "files": [
        {"path": "folder\\sample.png", "content_base64": base64.b64encode(payload).decode()},
        {"path": "folder\\sample.txt", "content_base64": base64.b64encode("a test caption café".encode()).decode()},
    ]})
    assert imported["state"] == "completed" and imported["imported"] == 1, imported
    asset = request("/api/assets?project_id=" + project["id"])[0]
    for variant in ({"kind": "content"}, {"kind": "thumbnail", "max_pixels": 256}):
        descriptor = request(f"/api/assets/{asset['id']}/delivery", {"asset_revision_id": asset["id"], "variant": variant})
        assert descriptor["kind"] == "descriptor", descriptor
        url = descriptor["descriptor"]["delivery_url"]
        content = request(url.removeprefix(origin))
        if variant["kind"] == "content":
            assert hashlib.sha256(content).digest() == hashlib.sha256(payload).digest()
        else:
            assert Image.open(io.BytesIO(content)).size == (96, 64)
    record("Project creation, Windows folder paths, Unicode captions, durable bytes, thumbnails")
    request("/api/operator-settings/fal-key", {"key": "local-smoke-placeholder"}, method="PUT")
    settings = request("/api/operator-settings")
    assert settings["fal_connection"]["configured"]
    assert "local-smoke-placeholder" not in json.dumps(settings)
    record("Write-only credential save and reload; no provider calls")
    duplicate = subprocess.Popen([str(bundle / "Modelfiche.exe"), "--no-browser"], cwd=EXTRACTED, env=env)
    assert duplicate.wait(timeout=90) == 0
    assert cli("status")["ok"]
    backup = cli("backup", "create")
    assert cli("backup", "verify", backup["archive"])["ok"]
    record("Duplicate-launch reuse and full backup verification")
    close_window(process.pid)
    assert process.wait(timeout=40) == 0
    assert cli("status", ok=False)["running"] is False
    record("Native window close stops all services")
    process, state = launch()
    assert request("/api/projects/" + project["id"])["title"] == "Windows smoke café"
    assert request("/api/assets?project_id=" + project["id"])[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert cli("stop")["ok"]
    assert process.wait(timeout=40) == 0
    record("Restart preserves workspace and imported bytes; CLI stop closes launcher")
    assert cli("backup", "restore", backup["archive"], "--confirm", "RESTORE MODELFICHE BACKUP")["ok"]
    record("Full backup restores after shutdown")
    process, state = launch()
    process.kill()
    process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while cli("status", ok=None)["running"] and time.monotonic() < deadline:
        time.sleep(0.5)
    assert cli("status", ok=False)["running"] is False
    record("Killing the native launcher reaps its service Job Object")
    (VALIDATION / "smoke-results.json").write_text(json.dumps({"ok": True, "checks": checks}, indent=2), encoding="utf-8")
finally:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=10)
    if (support / "logs").exists():
        shutil.copytree(support / "logs", VALIDATION / "logs", dirs_exist_ok=True)
    # Only publish test evidence as a CI artifact, never the disposable workspace.
    shutil.rmtree(EXTRACTED, ignore_errors=True)
    shutil.rmtree(support, ignore_errors=True)
    shutil.rmtree(support.parent / (support.name + "-backups"), ignore_errors=True)
