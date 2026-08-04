from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import models


_TOKEN = re.compile(r"^MF1_([A-Z2-7]+)_([A-Z2-7]+)$")
_ALLOWED_LAUNCH_STATES = frozenset({"preparing", "ready", "running", "completed"})


@dataclass(frozen=True, slots=True)
class WandbPrincipal:
    credential: models.WandbIngestCredential
    launch: models.TrainingLaunch
    run: models.TrainingRun
    claims: dict[str, object]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Basic realm="ModelFiche telemetry shim"'},
    )


def _decode_basic(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Basic "):
        raise _unauthorized(
            "Modelfiche launch token is required; a wandb.ai API key is not used"
        )
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _unauthorized("invalid Modelfiche launch-token credentials") from exc
    username, separator, launch_token = decoded.partition(":")
    if separator != ":" or username != "api" or len(launch_token) < 40:
        raise _unauthorized("invalid Modelfiche launch-token credentials")
    return launch_token


def _base32(value: str) -> bytes:
    try:
        return base64.b32decode(value + "=" * (-len(value) % 8), casefold=False)
    except binascii.Error as exc:
        raise _unauthorized("malformed Modelfiche launch token") from exc


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise _unauthorized(f"Modelfiche launch token is missing {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _unauthorized(f"Modelfiche launch token has invalid {name}") from exc
    if parsed.tzinfo is None:
        raise _unauthorized(f"Modelfiche launch token has invalid {name}")
    return parsed.astimezone(timezone.utc)


def authenticate_wandb_request(
    session: Session, authorization: str | None
) -> WandbPrincipal:
    launch_token = _decode_basic(authorization)
    match = _TOKEN.fullmatch(launch_token)
    if match is None:
        raise _unauthorized(
            "expected an MF1 Modelfiche launch token, not a wandb.ai API key"
        )
    payload = _base32(match.group(1))
    signature = _base32(match.group(2))
    try:
        claims = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unauthorized("malformed Modelfiche launch token claims") from exc
    if not isinstance(claims, dict):
        raise _unauthorized("malformed Modelfiche launch token claims")
    canonical = json.dumps(
        claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if payload != canonical:
        raise _unauthorized("Modelfiche launch token claims are not canonical")

    credential_id = claims.get("credential_id")
    launch_id = claims.get("launch_id")
    if not isinstance(credential_id, str) or not isinstance(launch_id, str):
        raise _unauthorized("Modelfiche launch token identity is incomplete")
    credential = session.get(models.WandbIngestCredential, credential_id)
    launch = session.get(models.TrainingLaunch, launch_id)
    if credential is None or launch is None:
        raise _unauthorized("Modelfiche launch token identity was not found")
    if credential.state != "active" or claims.get("kid") != credential.key_id:
        raise _unauthorized("Modelfiche signing credential is inactive or rotated")
    try:
        public_bytes = base64.urlsafe_b64decode(
            credential.public_key + "=" * (-len(credential.public_key) % 4)
        )
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature, payload)
    except (ValueError, InvalidSignature) as exc:
        raise _unauthorized("Modelfiche launch token signature is invalid") from exc

    run = session.get(models.TrainingRun, launch.run_id)
    if run is None:
        raise _unauthorized("Modelfiche launch run was not found")
    expected = {
        "claims_version": 1,
        "credential_id": credential.id,
        "kid": credential.key_id,
        "workspace_id": launch.workspace_id,
        "project_id": launch.project_id,
        "launch_id": launch.id,
        "run_id": run.id,
        "wandb_run_id": run.wandb_run_id,
    }
    if any(claims.get(name) != value for name, value in expected.items()):
        raise _unauthorized(
            "Modelfiche launch token scope does not match the expected run"
        )
    manifest_claims = (
        launch.redacted_manifest.get("telemetry", {}).get("token", {}).get("claims")
    )
    if claims != manifest_claims:
        raise _unauthorized(
            "Modelfiche launch token claims do not match the launch manifest"
        )
    now = models.utcnow()
    if _parse_time(claims.get("issued_at"), "issued_at") > now:
        raise _unauthorized("Modelfiche launch token is not active yet")
    if (
        _parse_time(claims.get("expires_at"), "expires_at") <= now
        and launch.state != "running"
    ):
        # A signed, manifest-pinned token remains scoped and revocable while
        # its run is active. Expected-duration estimates must not cut off
        # telemetry from a healthy long-running trainer.
        raise _unauthorized("Modelfiche launch token has expired")
    if launch.state not in _ALLOWED_LAUNCH_STATES:
        raise HTTPException(
            status_code=409, detail="training launch is not accepting W&B events"
        )
    credential.last_used_at = now
    return WandbPrincipal(credential=credential, launch=launch, run=run, claims=claims)
