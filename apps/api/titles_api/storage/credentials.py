from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


_ENV_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SourceCredentialsError(ValueError):
    """Raised before client construction when a source has no complete named credentials."""


def normalize_credential_env_prefix(prefix: str) -> str:
    normalized = prefix.strip().upper()
    if not _ENV_PREFIX.fullmatch(normalized):
        raise SourceCredentialsError("credential environment prefix must use uppercase letters, digits, and underscores")
    return normalized


@dataclass(frozen=True, slots=True)
class SourceCredentials:
    """In-memory credentials resolved only from one source's named environment variables."""

    env_prefix: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = field(default=None, repr=False)

    @classmethod
    def from_environ(
        cls,
        prefix: str,
        environ: Mapping[str, str] | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> "SourceCredentials":
        values = os.environ if environ is None else environ
        env_prefix = normalize_credential_env_prefix(prefix)
        access_key = values.get(f"{env_prefix}_ACCESS_KEY")
        secret_key = values.get(f"{env_prefix}_SECRET_KEY")
        if not access_key or not secret_key:
            raise SourceCredentialsError(
                f"{env_prefix}_ACCESS_KEY and {env_prefix}_SECRET_KEY are required for this source"
            )
        session_token = values.get(f"{env_prefix}_SESSION_TOKEN") or None
        return cls(
            env_prefix=env_prefix,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            expires_at=expires_at,
        )

    @property
    def epoch(self) -> str:
        """A non-secret, process-only identity that changes with any credential tuple change."""
        digest = hashlib.sha256()
        for value in (self.env_prefix, self.access_key, self.secret_key, self.session_token or "", self.expires_at.isoformat() if self.expires_at else ""):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return f"credential-v1:{digest.hexdigest()}"

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now
