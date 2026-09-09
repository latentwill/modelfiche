from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class CredentialError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialRegistration:
    alias: str
    public_key: str


def canonical_public_key(value: str) -> tuple[str, str]:
    encoded = value.strip()
    if not encoded:
        raise CredentialError("public_key is required")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise CredentialError("public_key must be an unpadded base64url Ed25519 public key") from exc
    if len(raw) != 32:
        raise CredentialError("public_key must encode exactly 32 bytes")
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    key_id = f"ed25519-{hashlib.sha256(raw).hexdigest()[:16]}"
    return canonical, key_id


def register_credential(session: Session, registration: CredentialRegistration) -> models.WandbIngestCredential:
    alias = registration.alias.strip()
    if not _ALIAS.fullmatch(alias):
        raise CredentialError("alias must start with a letter or digit and contain only letters, digits, '.', '_', or '-'")
    public_key, key_id = canonical_public_key(registration.public_key)
    credential = session.scalar(
        select(models.WandbIngestCredential).order_by(
            models.WandbIngestCredential.created_at,
            models.WandbIngestCredential.id,
        )
    )
    if credential is None:
        credential = models.WandbIngestCredential(
            alias=alias,
            algorithm="ed25519",
            key_id=key_id,
            public_key=public_key,
            state="active",
        )
        session.add(credential)
    else:
        key_changed = credential.public_key != public_key
        credential.alias = alias
        credential.public_key = public_key
        credential.key_id = key_id
        credential.state = "active"
        credential.revoked_at = None
        if key_changed:
            credential.rotated_at = models.utcnow()
    session.flush()
    return credential


def rotate_credential(
    session: Session,
    credential: models.WandbIngestCredential,
    public_key_value: str,
) -> models.WandbIngestCredential:
    public_key, key_id = canonical_public_key(public_key_value)
    key_changed = public_key != credential.public_key
    credential.public_key = public_key
    credential.key_id = key_id
    credential.state = "active"
    credential.revoked_at = None
    if key_changed:
        credential.rotated_at = models.utcnow()
    session.flush()
    return credential


def revoke_credential(session: Session, credential: models.WandbIngestCredential) -> models.WandbIngestCredential:
    if credential.state != "revoked":
        credential.state = "revoked"
        credential.revoked_at = models.utcnow()
        session.flush()
    return credential


def credential_projection(credential: models.WandbIngestCredential) -> dict[str, object]:
    return {
        "id": credential.id,
        "scope": "global",
        "alias": credential.alias,
        "algorithm": credential.algorithm,
        "key_id": credential.key_id,
        "public_key": credential.public_key,
        "state": credential.state,
        "created_at": credential.created_at,
        "updated_at": credential.updated_at,
        "rotated_at": credential.rotated_at,
        "revoked_at": credential.revoked_at,
        "last_used_at": credential.last_used_at,
    }
