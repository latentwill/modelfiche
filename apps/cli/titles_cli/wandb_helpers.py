from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


TOKEN_PLACEHOLDER = "<MF1_BASE32_ED25519_SIGNED_TOKEN>"


def _urlsafe_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_urlsafe_base64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def generate_keypair() -> tuple[str, str]:
    """Return URL-safe, unpadded raw Ed25519 private and public keys."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _urlsafe_base64(private_bytes), _urlsafe_base64(public_bytes)


def public_key_for_private(private_key_value: str) -> str:
    private_key = _private_key(private_key_value)
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _urlsafe_base64(public_bytes)


def sign_launch_claims(claims: Mapping[str, Any], private_key_value: str) -> str:
    payload = json.dumps(
        dict(claims),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = _private_key(private_key_value).sign(payload)
    encoded_payload = base64.b32encode(payload).decode("ascii").rstrip("=")
    encoded_signature = base64.b32encode(signature).decode("ascii").rstrip("=")
    return f"MF1_{encoded_payload}_{encoded_signature}"


def render_agent_environment(template: str, token: str) -> str:
    if template.count(TOKEN_PLACEHOLDER) != 1:
        raise ValueError("launch environment does not contain exactly one token placeholder")
    return template.replace(TOKEN_PLACEHOLDER, token)


def read_private_key(path: Path) -> str:
    try:
        value = path.expanduser().read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ValueError(f"cannot read private key from {path}: {exc}") from exc
    _private_key(value)
    return value


def write_private_key(path: Path, private_key_value: str) -> Path:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(private_key_value)
            handle.write("\n")
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return destination


def write_secret_text(path: Path, content: str) -> Path:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return destination


def _private_key(value: str) -> Ed25519PrivateKey:
    try:
        raw = _decode_urlsafe_base64(value.strip())
        if len(raw) != 32:
            raise ValueError("raw Ed25519 private keys must be 32 bytes")
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("private key must be an unpadded URL-safe base64 Ed25519 private key") from exc
