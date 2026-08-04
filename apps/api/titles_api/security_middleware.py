from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
import hmac
import ipaddress
import time
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable, Mapping

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[..., Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_REMOTE_SESSION_PATH = "/api/remote-session"
_REMOTE_SESSION_COOKIE = "modelfiche_remote_session"


@dataclass(frozen=True, slots=True)
class RemoteAccessContract:
    """Shared-secret authority for an authenticated loopback tunnel."""

    host: str
    origin: str
    client_id: str
    client_secret: str = field(repr=False, compare=False)
    session_ttl_seconds: int = 12 * 60 * 60

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != self.host
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "remote access origin must be the exact HTTPS origin for host"
            )
        if not self.client_id or not self.client_secret:
            raise ValueError("remote access credentials cannot be empty")
        if self.session_ttl_seconds <= 0:
            raise ValueError("remote access session TTL must be positive")


@dataclass(frozen=True, slots=True)
class LocalLaunchContract:
    """In-memory authority for the trusted single-user loopback listener."""

    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    allow_uds: bool = True
    remote_access: RemoteAccessContract | None = None
    synthetic_test_peers: frozenset[str] = frozenset()
    _authority: object = field(default_factory=object, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("at least one exact Host is required")
        if any(
            not value or "/" in value or "@" in value for value in self.allowed_hosts
        ):
            raise ValueError("allowed Hosts must be exact host:port authorities")
        if any(
            not value.startswith(("http://", "https://"))
            for value in self.allowed_origins
        ):
            raise ValueError("allowed Origins must include an http(s) scheme")


class TrustedLocalBoundary:
    """Admit local callers and explicitly authenticated loopback-tunnel callers."""

    def __init__(self, app: ASGIApp, contract: LocalLaunchContract) -> None:
        self.app = app
        self.contract = contract

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        status, remote = self._rejection_status(scope)
        if status is not None:
            await self._reject(scope, send, status)
            return
        if remote and scope.get("path") == _REMOTE_SESSION_PATH:
            await self._remote_session_response(scope, send)
            return
        await self.app(scope, receive, send)

    def _rejection_status(self, scope: Mapping[str, Any]) -> tuple[int | None, bool]:
        client = scope.get("client")
        if client is None:
            if not self.contract.allow_uds:
                return 403, False
        else:
            peer_text = str(client[0]).split("%", 1)[0]
            try:
                peer = ipaddress.ip_address(peer_text)
            except (ValueError, TypeError, IndexError):
                if peer_text not in self.contract.synthetic_test_peers:
                    return 403, False
            else:
                if not peer.is_loopback:
                    return 403, False

        normalized = self._normalized_headers(scope)
        if normalized is None:
            return 400, False
        remote = any(
            name == "forwarded" or name.startswith("x-forwarded-")
            for name, _ in normalized
        )
        if remote:
            return self._remote_rejection_status(scope, normalized), True
        return self._local_rejection_status(scope, normalized), False

    def _local_rejection_status(
        self, scope: Mapping[str, Any], headers: list[tuple[str, str]]
    ) -> int | None:
        hosts = self._values(headers, "host")
        if len(hosts) != 1 or hosts[0].lower() not in self.contract.allowed_hosts:
            return 400
        origins = self._values(headers, "origin")
        if len(origins) > 1:
            return 400
        if origins and origins[0] not in self.contract.allowed_origins:
            return 403
        if self._is_mutation(scope) and len(origins) != 1:
            return 403
        return None

    def _remote_rejection_status(
        self, scope: Mapping[str, Any], headers: list[tuple[str, str]]
    ) -> int | None:
        remote = self.contract.remote_access
        if remote is None:
            return 400
        hosts = self._values(headers, "host")
        if hosts != [remote.host]:
            return 400
        if self._values(headers, "x-forwarded-proto") != ["https"]:
            return 400
        if len(self._values(headers, "x-forwarded-for")) != 1:
            return 400
        if (
            len(self._values(headers, "cf-connecting-ip")) != 1
            or len(self._values(headers, "cf-ray")) != 1
        ):
            return 400

        origins = self._values(headers, "origin")
        if len(origins) > 1:
            return 400
        if origins and origins[0] != remote.origin:
            return 403
        if self._is_mutation(scope) and origins != [remote.origin]:
            return 403

        has_credentials = self._has_remote_credentials(headers, remote)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if path == _REMOTE_SESSION_PATH and method == "POST":
            return None if has_credentials else 401
        if path == _REMOTE_SESSION_PATH and method == "DELETE":
            return (
                None
                if has_credentials or self._has_valid_remote_session(headers, remote)
                else 401
            )
        return (
            None
            if has_credentials or self._has_valid_remote_session(headers, remote)
            else 401
        )

    @staticmethod
    def _normalized_headers(scope: Mapping[str, Any]) -> list[tuple[str, str]] | None:
        normalized: list[tuple[str, str]] = []
        for raw_name, raw_value in list(scope.get("headers") or ()):
            try:
                name = raw_name.decode("ascii").lower()
                value = raw_value.decode("latin-1").strip()
            except UnicodeError:
                return None
            normalized.append((name, value))
        return normalized

    @staticmethod
    def _values(headers: list[tuple[str, str]], name: str) -> list[str]:
        return [value for header_name, value in headers if header_name == name]

    @staticmethod
    def _is_mutation(scope: Mapping[str, Any]) -> bool:
        return str(scope.get("method", "GET")).upper() in _MUTATING_METHODS

    def _has_remote_credentials(
        self, headers: list[tuple[str, str]], remote: RemoteAccessContract
    ) -> bool:
        client_ids = self._values(headers, "cf-access-client-id")
        client_secrets = self._values(headers, "cf-access-client-secret")
        return (
            len(client_ids) == 1
            and len(client_secrets) == 1
            and hmac.compare_digest(client_ids[0], remote.client_id)
            and hmac.compare_digest(client_secrets[0], remote.client_secret)
        )

    def _has_valid_remote_session(
        self, headers: list[tuple[str, str]], remote: RemoteAccessContract
    ) -> bool:
        cookie_headers = self._values(headers, "cookie")
        if len(cookie_headers) != 1:
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_headers[0])
        except CookieError:
            return False
        morsel = cookies.get(_REMOTE_SESSION_COOKIE)
        if morsel is None:
            return False
        try:
            version, raw_expiry, signature = morsel.value.split(".", 2)
            expiry = int(raw_expiry)
        except (TypeError, ValueError):
            return False
        if version != "v1" or expiry < int(time.time()):
            return False
        payload = f"{version}.{expiry}"
        expected = self._session_signature(payload, remote.client_secret)
        return hmac.compare_digest(signature, expected)

    async def _remote_session_response(
        self,
        scope: Mapping[str, Any],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        remote = self.contract.remote_access
        if remote is None:
            await self._reject(scope, send, 400)
            return
        method = str(scope.get("method", "GET")).upper()
        if method == "POST":
            expiry = int(time.time()) + remote.session_ttl_seconds
            payload = f"v1.{expiry}"
            token = (
                f"{payload}.{self._session_signature(payload, remote.client_secret)}"
            )
            cookie = (
                f"{_REMOTE_SESSION_COOKIE}={token}; Path=/api; Max-Age={remote.session_ttl_seconds}; "
                "HttpOnly; Secure; SameSite=Strict"
            )
            body = b'{"authenticated":true}'
        elif method == "DELETE":
            cookie = f"{_REMOTE_SESSION_COOKIE}=; Path=/api; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
            body = b'{"authenticated":false}'
        else:
            await self._reject(scope, send, 405)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"set-cookie", cookie.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _session_signature(payload: str, secret: str) -> str:
        signature = hmac.digest(secret.encode("utf-8"), payload.encode("ascii"), sha256)
        return urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    @staticmethod
    async def _reject(
        scope: Mapping[str, Any],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
    ) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        detail = (
            b"remote authentication required"
            if status == 401
            else b"local request boundary rejected"
        )
        body = b'{"detail":"' + detail + b'"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
