from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from typing import Any, Awaitable, Callable, Mapping

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class LocalLaunchContract:
    """In-memory authority for the trusted single-user loopback listener."""

    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    allow_uds: bool = True
    synthetic_test_peers: frozenset[str] = frozenset()
    _authority: object = field(default_factory=object, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("at least one exact Host is required")
        if any(not value or "/" in value or "@" in value for value in self.allowed_hosts):
            raise ValueError("allowed Hosts must be exact host:port authorities")
        if any(not value.startswith(("http://", "https://")) for value in self.allowed_origins):
            raise ValueError("allowed Origins must include an http(s) scheme")


class TrustedLocalBoundary:
    """Reject requests that do not originate at the launcher-owned local boundary."""

    def __init__(self, app: ASGIApp, contract: LocalLaunchContract) -> None:
        self.app = app
        self.contract = contract

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        status = self._rejection_status(scope)
        if status is not None:
            await self._reject(scope, send, status)
            return
        await self.app(scope, receive, send)

    def _rejection_status(self, scope: Mapping[str, Any]) -> int | None:
        client = scope.get("client")
        if client is None:
            if not self.contract.allow_uds:
                return 403
        else:
            peer_text = str(client[0]).split("%", 1)[0]
            try:
                peer = ipaddress.ip_address(peer_text)
            except (ValueError, TypeError, IndexError):
                if peer_text not in self.contract.synthetic_test_peers:
                    return 403
            else:
                if not peer.is_loopback:
                    return 403

        headers: list[tuple[bytes, bytes]] = list(scope.get("headers") or ())
        normalized: list[tuple[str, str]] = []
        for raw_name, raw_value in headers:
            try:
                name = raw_name.decode("ascii").lower()
                value = raw_value.decode("latin-1").strip()
            except UnicodeError:
                return 400
            if name == "forwarded" or name.startswith("x-forwarded-"):
                return 400
            normalized.append((name, value))

        hosts = [value.lower() for name, value in normalized if name == "host"]
        if len(hosts) != 1 or hosts[0] not in self.contract.allowed_hosts:
            return 400

        origins = [value for name, value in normalized if name == "origin"]
        if len(origins) > 1:
            return 400
        if origins and origins[0] not in self.contract.allowed_origins:
            return 403
        if str(scope.get("method", "GET")).upper() in _MUTATING_METHODS and len(origins) != 1:
            return 403
        return None

    @staticmethod
    async def _reject(scope: Mapping[str, Any], send: Callable[[dict[str, Any]], Awaitable[None]], status: int) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"local request boundary rejected"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})
