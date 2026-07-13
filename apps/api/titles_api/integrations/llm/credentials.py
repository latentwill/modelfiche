from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROVIDER_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "openrouter": "openrouter",
    "open-router": "openrouter",
    "open_router": "openrouter",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}


@dataclass(frozen=True, slots=True)
class LlmCredential:
    key: str
    source: str


class LlmSecretStore:
    """Write-only local LLM credential boundary; callers only receive status/source."""

    def __init__(self, config_root: Path):
        self.root = config_root / "secrets"
        self.path = self.root / "llm-key"
        self.status_path = config_root / "llm-status.json"

    def save(self, key: str) -> None:
        value = key.strip()
        if not value:
            raise ValueError("LLM key cannot be empty")
        self._atomic_write(self.path, value.encode("utf-8"), 0o600)
        self._record("configuration_only", None)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._record("missing", None)

    def resolve(self, provider: str | None = None, environ: dict[str, str] | os._Environ[str] | None = None) -> LlmCredential | None:
        env = os.environ if environ is None else environ
        if provider == "__saved__":
            names: tuple[str, ...] = ()
        else:
            normalized = _PROVIDER_ALIASES.get(str(provider or "").strip().lower())
            names = {
                "openai": ("LLM_API_KEY", "OPENAI_API_KEY"),
                "openai-compatible": ("LLM_API_KEY", "OPENAI_API_KEY"),
                "openrouter": ("OPENROUTER_API_KEY", "LLM_API_KEY"),
                "anthropic": ("LLM_API_KEY", "ANTHROPIC_API_KEY"),
                "gemini": ("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
                None: ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
            }.get(normalized, ("LLM_API_KEY",))
        for name in names:
            value = env.get(name)
            if value and str(value).strip():
                return LlmCredential(str(value), name)
        key = self._read_key()
        return LlmCredential(key, "saved") if key else None

    def status(self, provider: str | None = None) -> dict[str, Any]:
        credential = self.resolve(provider)
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
        self._record(state, error)

    def _record(self, state: str, error: str | None) -> None:
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

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
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


def llm_secret_store() -> LlmSecretStore:
    from ...settings import get_settings

    return LlmSecretStore(get_settings().config_root)


def resolve_llm_credential(provider: str | None = None) -> LlmCredential | None:
    return llm_secret_store().resolve(provider)
