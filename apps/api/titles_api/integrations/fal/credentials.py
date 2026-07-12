from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FalCredential:
    key: str
    source: str


class FalSecretStore:
    """Small write-only credential boundary; callers never receive stored secrets."""

    def __init__(self, config_root: Path):
        self.root = config_root / "secrets"
        self.path = self.root / "fal-key"
        self.status_path = config_root / "fal-status.json"

    def save(self, key: str) -> None:
        value = key.strip()
        if not value:
            raise ValueError("FAL key cannot be empty")
        self._atomic_write(self.path, value.encode("utf-8"), 0o600)
        self.record_validation("configuration_only", None)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.record_validation("missing", None)

    def configured(self) -> bool:
        return bool(self._read_key())

    def resolve(self, environ: dict[str, str] | os._Environ[str] | None = None) -> FalCredential | None:
        env = os.environ if environ is None else environ
        if env.get("FAL_KEY"):
            return FalCredential(str(env["FAL_KEY"]), "FAL_KEY")
        if env.get("FAL_API_KEY"):
            return FalCredential(str(env["FAL_API_KEY"]), "FAL_API_KEY")
        key = self._read_key()
        return FalCredential(key, "saved") if key else None

    def status(self) -> dict[str, Any]:
        credential = self.resolve()
        result: dict[str, Any] = {
            "configured": credential is not None,
            "source": credential.source if credential else None,
            "last_validation": None,
        }
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                result["last_validation"] = raw
        except (FileNotFoundError, OSError, ValueError):
            pass
        return result

    def record_validation(self, state: str, error: str | None) -> None:
        payload = {
            "state": state,
            "network_validated": state == "validated",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }
        self._atomic_write(self.status_path, json.dumps(payload, separators=(",", ":")).encode(), 0o600)

    def _read_key(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        return value or None

    def _atomic_write(self, path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(mode)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def fal_secret_store() -> FalSecretStore:
    # Local import avoids settings/config import cycles.
    from ...settings import get_settings

    return FalSecretStore(get_settings().config_root)


def resolve_fal_credential() -> FalCredential | None:
    return fal_secret_store().resolve()
