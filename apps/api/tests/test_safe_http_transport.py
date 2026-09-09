from __future__ import annotations

import io
import json

import pytest

from titles_api.integrations.config import FalSettings
from titles_api.integrations.fal.client import FalApiTransport, FalProviderError, FalQueueClient
from titles_api.storage.network import (
    ArtifactFetchTransport,
    HttpResponse,
    NetworkPolicyError,
    SafeHttpTransport,
    StrictJsonError,
    decode_strict_json,
)


class ScriptedExchange:
    def __init__(self, responses: list[HttpResponse], peers: list[str] | None = None):
        self.responses = list(responses)
        self.peers = list(peers or ["93.184.216.34"] * len(responses))
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method, url, headers, body, _timeouts):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0), self.peers.pop(0)


def resolver_for(*answers: str):
    def resolve(_host: str, _port: int):
        return tuple(answers)
    return resolve


def test_rejects_private_resolution_before_exchange():
    exchange = ScriptedExchange([])
    transport = SafeHttpTransport(resolver=resolver_for("10.0.0.5"), exchange=exchange)
    with pytest.raises(NetworkPolicyError, match="public"):
        transport.request("GET", "https://example.test/file")
    assert exchange.calls == []


def test_rejects_actual_private_peer_even_after_public_dns():
    exchange = ScriptedExchange([HttpResponse(200, {}, b"ok")], peers=["127.0.0.1"])
    transport = SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange)
    with pytest.raises(NetworkPolicyError, match="peer"):
        transport.request("GET", "https://example.test/file")


def test_revalidates_redirect_and_rejects_public_to_private_rebinding():
    exchange = ScriptedExchange([HttpResponse(302, {"location": "https://cdn.example.test/file"}, b"")])
    answers = iter([("93.184.216.34",), ("169.254.169.254",)])
    transport = SafeHttpTransport(resolver=lambda _h, _p: next(answers), exchange=exchange)
    with pytest.raises(NetworkPolicyError, match="public"):
        transport.request("GET", "https://example.test/file", allow_redirects=True)
    assert len(exchange.calls) == 1


def test_proxy_environment_is_never_consulted(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    exchange = ScriptedExchange([HttpResponse(200, {}, b"ok")])
    transport = SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange)
    response = transport.request("GET", "https://example.test/file")
    assert response.body == b"ok"
    assert response.status == 200
    assert exchange.calls[0][1] == "https://example.test/file"


def test_dns_resolution_errors_are_transport_policy_errors():
    import socket

    def resolver(_host, _port, **_kwargs):
        raise socket.gaierror("unreachable")

    transport = SafeHttpTransport(resolver=resolver, exchange=ScriptedExchange([]))
    with pytest.raises(NetworkPolicyError, match="resolution failed"):
        transport.request("GET", "https://example.test/file")


def test_fal_client_rejects_cross_origin_lifecycle_url_before_request():
    exchange = ScriptedExchange([])
    transport = SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange)
    client = FalQueueClient(
        FalSettings("secret", "https://queue.fal.run"),
        transport=FalApiTransport(FalSettings("secret", "https://queue.fal.run"), transport=transport),
    )
    with pytest.raises(FalProviderError, match="canonical API origin"):
        client.status("https://attacker.example/requests/request-1/status")
    assert exchange.calls == []


def test_fal_submit_accepts_only_matching_canonical_provider_lifecycle_urls():
    body = json.dumps({
        "request_id": "req_123",
        "status_url": "https://queue.fal.run/fal-ai/canonical/requests/req_123/status",
        "response_url": "https://queue.fal.run/fal-ai/canonical/requests/req_123",
        "cancel_url": "https://queue.fal.run/fal-ai/canonical/requests/req_123/cancel",
    }).encode()
    exchange = ScriptedExchange([HttpResponse(200, {}, body)])
    settings = FalSettings("secret", "https://queue.fal.run")
    client = FalQueueClient(
        settings,
        transport=FalApiTransport(
            settings,
            transport=SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange),
        ),
    )
    submission = client.submit("fal-ai/model", {"prompt": "x"})
    assert submission.status_url == "https://queue.fal.run/fal-ai/canonical/requests/req_123/status"
    assert submission.response_url == "https://queue.fal.run/fal-ai/canonical/requests/req_123"
    assert exchange.calls[0][2]["Authorization"] == "Key secret"


def test_fal_submit_rejects_cross_origin_provider_lifecycle_urls():
    body = json.dumps({"request_id": "req_123", "status_url": "https://attacker.test/steal"}).encode()
    exchange = ScriptedExchange([HttpResponse(200, {}, body)])
    settings = FalSettings("secret", "https://queue.fal.run")
    client = FalQueueClient(
        settings,
        transport=FalApiTransport(
            settings,
            transport=SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange),
        ),
    )
    with pytest.raises(FalProviderError, match="canonical API origin"):
        client.submit("fal-ai/model", {"prompt": "x"})


def test_strict_json_rejects_duplicate_keys_and_shape_bounds():
    with pytest.raises(StrictJsonError, match="duplicate"):
        decode_strict_json(b'{"a":1,"a":2}')
    with pytest.raises(StrictJsonError, match="depth"):
        decode_strict_json(json.dumps({"root": json.loads("[" * 17 + "0" + "]" * 17)}).encode())
    with pytest.raises(StrictJsonError, match="list"):
        decode_strict_json(json.dumps({"items": list(range(101))}).encode(), max_list_items=100)
    with pytest.raises(StrictJsonError, match="string"):
        decode_strict_json(json.dumps({"value": "x" * 33}).encode(), max_string_bytes=32)
    with pytest.raises(StrictJsonError, match="key limit"):
        decode_strict_json(json.dumps({str(i): i for i in range(513)}).encode())


def test_fal_response_bound_is_enforced_before_json_decode():
    exchange = ScriptedExchange([HttpResponse(200, {}, b"{" + b"x" * (2 * 1024 * 1024))])
    settings = FalSettings("secret", "https://queue.fal.run")
    client = FalQueueClient(
        settings,
        transport=FalApiTransport(
            settings,
            transport=SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange),
        ),
    )
    with pytest.raises(FalProviderError, match="response exceeded"):
        client.submit("fal-ai/model", {})


def test_artifact_transport_never_forwards_credentials_across_redirect():
    exchange = ScriptedExchange([
        HttpResponse(302, {"location": "https://cdn.example.test/artifact"}, b""),
        HttpResponse(200, {}, b"image"),
    ])
    transport = ArtifactFetchTransport(
        SafeHttpTransport(resolver=resolver_for("93.184.216.34"), exchange=exchange)
    )
    target = io.BytesIO()
    result = transport.fetch_to("https://files.example.test/start", target, max_bytes=10)
    assert result.size == 5
    assert target.getvalue() == b"image"
    assert all("Authorization" not in headers and "Cookie" not in headers for _, _, headers, _ in exchange.calls)
