from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import zipfile

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import __version__, models

_SECRET_ASSIGNMENT = re.compile(r"(?i)(authorization|api[-_ ]?key|secret|token|password|credential)(\s*[:=]\s*)([^\s,;&]+)")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")


def _redact_text(value: str, home: Path) -> str:
    redacted = value.replace(str(home), "~")
    redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    return _AWS_ACCESS_KEY.sub("[REDACTED_AWS_ACCESS_KEY]", redacted)


def _read_log_tail(path: Path, home: Path, maximum_bytes: int = 256 * 1024) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - maximum_bytes))
        payload = handle.read()
    return _redact_text(payload.decode("utf-8", errors="replace"), home)


def create_support_bundle(db: Session, *, database_url: str, asset_root: Path, cache_root: Path, export_root: Path) -> bytes:
    home = Path.home()
    try:
        migration = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:
        migration = None
    jobs = list(db.scalars(select(models.Job).order_by(models.Job.updated_at.desc()).limit(50)))
    heartbeats = list(db.scalars(select(models.WorkerHeartbeat).order_by(models.WorkerHeartbeat.heartbeat_at.desc()).limit(5)))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": __version__,
        "database": {"backend": "sqlite" if database_url.startswith("sqlite") else "configured", "migration": migration},
        "storage": {
            "asset_root": {"path": _redact_text(str(asset_root), home), "available": asset_root.exists()},
            "cache_root": {"path": _redact_text(str(cache_root), home), "available": cache_root.exists()},
            "export_root": {"path": _redact_text(str(export_root), home), "available": export_root.exists()},
        },
        "jobs": {
            "counts": dict(Counter(job.state.value for job in jobs)),
            "recent": [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "state": job.state.value,
                    "attempts": job.attempts,
                    "error": _redact_text(job.error, home) if job.error else None,
                    "updated_at": job.updated_at,
                }
                for job in jobs
            ],
        },
        "workers": [
            {
                "worker_id": heartbeat.worker_id,
                "state": heartbeat.state,
                "version": heartbeat.version,
                "started_at": heartbeat.started_at,
                "heartbeat_at": heartbeat.heartbeat_at,
            }
            for heartbeat in heartbeats
        ],
        "redaction": {
            "credentials_included": False,
            "request_payloads_included": False,
            "database_contents_included": False,
            "home_directory_replaced": True,
        },
    }
    log_root = Path(os.getenv("TITLES_LOG_ROOT", str(home / "Library" / "Logs" / "Modelfiche"))).expanduser()
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("report.json", json.dumps(report, indent=2, default=str, sort_keys=True) + "\n")
        for name in ("api.log", "worker.log", "wandb-ingress.log"):
            tail = _read_log_tail(log_root / name, home)
            if tail:
                bundle.writestr(f"logs/{name}", tail)
    return archive.getvalue()
