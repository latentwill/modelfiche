from __future__ import annotations

import asyncio
from collections.abc import Iterable

from titles_api.security_middleware import (
    LocalLaunchContract,
    RemoteAccessContract,
    TrustedLocalBoundary,
)


def local_contract(
    *, remote_access: RemoteAccessContract | None = None
) -> LocalLaunchContract:
    return LocalLaunchContract(
        allowed_hosts=frozenset({"127.0.0.1:8400", "localhost:8400"}),
        allowed_origins=frozenset({"http://127.0.0.1:5173"}),
        remote_access=remote_access,
    )


def request_messages(
    *,
    peer: str = "127.0.0.1",
    headers: Iterable[tuple[bytes, bytes]] = ((b"host", b"127.0.0.1:8400"),),
    method: str = "GET",
    path: str = "/api/test",
    contract: LocalLaunchContract | None = None,
) -> list[dict]:
    messages: list[dict] = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "client": (peer, 40000),
        "headers": list(headers),
    }
    asyncio.run(
        TrustedLocalBoundary(downstream, contract or local_contract())(
            scope, receive, send
        )
    )
    return messages


def request_status(**kwargs) -> int:
    return next(
        message["status"]
        for message in request_messages(**kwargs)
        if message["type"] == "http.response.start"
    )


def test_accepts_exact_loopback_host() -> None:
    assert request_status() == 204


def test_rejects_nonloopback_peer() -> None:
    assert request_status(peer="192.168.1.10") == 403


def test_rejects_forwarded_headers_case_insensitively() -> None:
    assert (
        request_status(
            headers=((b"host", b"127.0.0.1:8400"), (b"X-Forwarded-For", b"127.0.0.1"))
        )
        == 400
    )


def test_rejects_duplicate_or_wrong_host() -> None:
    assert (
        request_status(
            headers=((b"host", b"127.0.0.1:8400"), (b"host", b"localhost:8400"))
        )
        == 400
    )
    assert request_status(headers=((b"host", b"evil.example"),)) == 400


def test_requires_exact_origin_for_mutation() -> None:
    assert request_status(method="POST") == 403
    assert (
        request_status(
            method="POST",
            headers=(
                (b"host", b"127.0.0.1:8400"),
                (b"origin", b"http://127.0.0.1:5173"),
            ),
        )
        == 204
    )


def remote_contract() -> LocalLaunchContract:
    return local_contract(
        remote_access=RemoteAccessContract(
            host="modelfiche.example.com",
            origin="https://modelfiche.example.com",
            client_id="client-id",
            client_secret="client-secret",
        )
    )


def remote_headers(
    *,
    credentials: tuple[str, str] | None = None,
    cookie: str | None = None,
    origin: bool = False,
) -> tuple[tuple[bytes, bytes], ...]:
    headers = [
        (b"host", b"modelfiche.example.com"),
        (b"x-forwarded-for", b"203.0.113.10"),
        (b"x-forwarded-proto", b"https"),
        (b"cf-connecting-ip", b"203.0.113.10"),
        (b"cf-ray", b"test-SIN"),
    ]
    if origin:
        headers.append((b"origin", b"https://modelfiche.example.com"))
    if credentials:
        headers.extend(
            (
                (b"cf-access-client-id", credentials[0].encode()),
                (b"cf-access-client-secret", credentials[1].encode()),
            )
        )
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return tuple(headers)


def test_remote_tunnel_requires_configured_credentials() -> None:
    assert request_status(headers=remote_headers(), contract=remote_contract()) == 401
    assert (
        request_status(
            headers=remote_headers(credentials=("client-id", "wrong")),
            contract=remote_contract(),
        )
        == 401
    )
    assert (
        request_status(
            headers=remote_headers(credentials=("client-id", "client-secret")),
            contract=remote_contract(),
        )
        == 204
    )


def test_remote_browser_session_authenticates_subsequent_requests() -> None:
    contract = remote_contract()
    messages = request_messages(
        method="POST",
        path="/api/remote-session",
        headers=remote_headers(credentials=("client-id", "client-secret"), origin=True),
        contract=contract,
    )

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert start["status"] == 200
    set_cookie = dict(start["headers"])[b"set-cookie"].decode()
    assert (
        "HttpOnly" in set_cookie
        and "Secure" in set_cookie
        and "SameSite=Strict" in set_cookie
    )
    cookie = set_cookie.split(";", 1)[0]
    assert (
        request_status(headers=remote_headers(cookie=cookie), contract=contract) == 204
    )
    assert (
        request_status(
            method="POST",
            headers=remote_headers(cookie=cookie, origin=True),
            contract=contract,
        )
        == 204
    )


def test_remote_mutation_requires_exact_public_origin() -> None:
    credentials = ("client-id", "client-secret")
    assert (
        request_status(
            method="POST",
            headers=remote_headers(credentials=credentials),
            contract=remote_contract(),
        )
        == 403
    )
