from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any


# A local deployment may provide a stable secret so tokens survive API process
# restarts. The random fallback deliberately scopes tokens to this process when
# no deployment secret has been configured.
_SECRET = os.getenv("TITLES_OPERATION_REVIEW_SECRET", "").encode("utf-8") or secrets.token_bytes(32)


def _encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_review_token(kind: str, scope: dict[str, Any], *, ttl_seconds: int = 900) -> str:
    if ttl_seconds <= 0:
        raise ValueError("review token lifetime must be positive")
    payload = {"v": 1, "kind": kind, "scope": scope, "exp": int(time.time()) + ttl_seconds, "nonce": secrets.token_urlsafe(10)}
    encoded = _b64(_encode(payload))
    signature = hmac.new(_SECRET, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def verify_review_token(token: str, kind: str, scope: dict[str, Any]) -> tuple[bool, str]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64(hmac.new(_SECRET, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False, "review token signature is invalid"
        payload = json.loads(_unb64(encoded))
        if payload.get("v") != 1 or payload.get("kind") != kind:
            return False, "review token type is invalid"
        if int(payload.get("exp", 0)) < int(time.time()):
            return False, "review token has expired"
        if payload.get("scope") != scope:
            return False, "review token does not match the reviewed scope"
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error, OverflowError):
        return False, "review token is malformed"
    return True, ""


# Keep binascii imported after the helper to avoid exposing implementation
# details to API callers while still handling malformed base64 safely.
import binascii  # noqa: E402  (used by verify_review_token)
