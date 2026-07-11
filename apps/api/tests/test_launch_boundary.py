from __future__ import annotations

import asyncio
from collections.abc import Iterable

from titles_api.security_middleware import LocalLaunchContract, TrustedLocalBoundary


def request_status(
    *,
    peer: str = "127.0.0.1",
    headers: Iterable[tuple[bytes, bytes]] = ((b"host", b"127.0.0.1:8400"),),
    method: str = "GET",
) -> int:
    status = 0

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {
        "type": "http",
        "method": method,
        "path": "/api/test",
        "client": (peer, 40000),
        "headers": list(headers),
    }
    contract = LocalLaunchContract(
        allowed_hosts=frozenset({"127.0.0.1:8400", "localhost:8400"}),
        allowed_origins=frozenset({"http://127.0.0.1:5173"}),
    )
    asyncio.run(TrustedLocalBoundary(downstream, contract)(scope, receive, send))
    return status


def test_accepts_exact_loopback_host() -> None:
    assert request_status() == 204


def test_rejects_nonloopback_peer() -> None:
    assert request_status(peer="192.168.1.10") == 403


def test_rejects_forwarded_headers_case_insensitively() -> None:
    assert request_status(headers=((b"host", b"127.0.0.1:8400"), (b"X-Forwarded-For", b"127.0.0.1"))) == 400


def test_rejects_duplicate_or_wrong_host() -> None:
    assert request_status(headers=((b"host", b"127.0.0.1:8400"), (b"host", b"localhost:8400"))) == 400
    assert request_status(headers=((b"host", b"evil.example"),)) == 400


def test_requires_exact_origin_for_mutation() -> None:
    assert request_status(method="POST") == 403
    assert request_status(
        method="POST",
        headers=((b"host", b"127.0.0.1:8400"), (b"origin", b"http://127.0.0.1:5173")),
    ) == 204
