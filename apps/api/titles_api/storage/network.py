from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class NetworkPolicyError(ValueError):
    """A request violated the outbound transport policy before a body was accepted."""


class ResponseLimitError(NetworkPolicyError):
    pass


class JsonShapeError(NetworkPolicyError):
    pass
StrictJsonError = JsonShapeError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Small exchange-compatible response used by deterministic transport tests."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def status(self) -> int:
        return self.status_code


@dataclass(frozen=True, slots=True)
class ArtifactFetchResult:
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: str
    host: str
    port: int
    addresses: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]


@dataclass(frozen=True, slots=True)
class SafeHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    peer: ipaddress.IPv4Address | ipaddress.IPv6Address
    @property
    def status(self) -> int:
        """Compatibility spelling shared with the exchange response."""
        return self.status_code


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global


def _address_from_sockaddr(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, tuple) and value:
        return str(value[0])
    raise NetworkPolicyError("outbound transport returned an invalid peer address")


def _resolve_addresses(
    host: str,
    port: int,
    resolver: Callable[..., Any],
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        try:
            records = resolver(host, port, type=socket.SOCK_STREAM)
        except TypeError:
            records = resolver(host, port)
    except OSError as exc:
        raise NetworkPolicyError("outbound host resolution failed") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in records:
        sockaddr = record[4] if isinstance(record, tuple) and len(record) >= 5 else record
        try:
            address = ipaddress.ip_address(_address_from_sockaddr(sockaddr))
        except ValueError as exc:
            raise NetworkPolicyError("outbound host resolution returned a non-IP address") from exc
        if not _is_public(address):
            raise NetworkPolicyError("outbound host resolved to a non-public address")
        addresses.add(address)
    if not addresses:
        raise NetworkPolicyError("outbound host did not resolve to a public address")
    return frozenset(addresses)


def validate_https_url(
    url: str,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> ValidatedUrl:
    """Validate an HTTPS URL and every A/AAAA answer before connecting."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise NetworkPolicyError("outbound URL has an invalid port") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise NetworkPolicyError("outbound URL must be HTTPS without userinfo or a fragment")
    host = parsed.hostname.rstrip(".").lower()
    return ValidatedUrl(url=url, host=host, port=port, addresses=_resolve_addresses(host, port, resolver))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonShapeError("provider response has duplicate JSON keys")
        result[key] = value
    return result


def decode_bounded_json(
    body: bytes,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_depth: int = 16,
    max_keys: int = 512,
    max_list_items: int = 100,
    max_string_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """Decode bounded provider JSON before exposing it to application code."""
    if min(max_bytes, max_depth, max_keys, max_list_items, max_string_bytes) < 0:
        raise ValueError("JSON bounds must be non-negative")
    if len(body) > max_bytes:
        raise ResponseLimitError("provider response exceeded the configured byte limit")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(JsonShapeError("provider response has an invalid JSON constant")),
        )
    except UnicodeDecodeError as exc:
        raise JsonShapeError("provider response is not UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise JsonShapeError("provider response is malformed JSON") from exc
    except RecursionError as exc:
        raise JsonShapeError("provider response exceeds the configured JSON depth") from exc
    key_count = 0

    def inspect(item: Any, depth: int) -> None:
        nonlocal key_count
        if depth > max_depth:
            raise JsonShapeError("provider response exceeds the configured JSON depth")
        if isinstance(item, str):
            if len(item.encode("utf-8")) > max_string_bytes:
                raise JsonShapeError("provider response has an oversized JSON string")
            return
        if isinstance(item, dict):
            key_count += len(item)
            if key_count > max_keys:
                raise JsonShapeError("provider response exceeds the configured JSON key limit")
            for key, child in item.items():
                if len(key.encode("utf-8")) > max_string_bytes:
                    raise JsonShapeError("provider response has an oversized JSON key")
                inspect(child, depth + 1)
            return
        if isinstance(item, list):
            if len(item) > max_list_items:
                raise JsonShapeError("provider response exceeds the configured JSON list limit")
            for child in item:
                inspect(child, depth + 1)

    try:
        inspect(value, 0)
    except RecursionError as exc:
        raise JsonShapeError("provider response exceeds the configured JSON depth") from exc
    if not isinstance(value, dict):
        raise JsonShapeError("provider response must have a JSON object at the top level")
    return value


decode_strict_json = decode_bounded_json


class SafeHttpTransport:
    """HTTPS-only, proxy-free HTTP transport with DNS-rebinding peer verification."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        client_factory: Callable[[], httpx.Client] | None = None,
        peer_getter: Callable[[httpx.Response], Any] | None = None,
        exchange: Callable[[str, str, Mapping[str, str], bytes | None, tuple[float, float, float]], tuple[HttpResponse, Any]] | None = None,
        max_redirects: int = 3,
    ):
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        self._resolver = resolver
        self._client_factory = client_factory or (lambda: httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(60.0, connect=10.0, read=30.0)))
        self._peer_getter = peer_getter or self._connected_peer
        self._exchange = exchange
        self._max_redirects = max_redirects

    @staticmethod
    def _connected_peer(response: httpx.Response) -> Any:
        stream = response.extensions.get("network_stream")
        if stream is None:
            raise NetworkPolicyError("outbound transport did not expose the connected peer")
        peer = stream.get_extra_info("server_addr")
        if peer is None:
            raise NetworkPolicyError("outbound transport did not expose the connected peer")
        return peer

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        max_error_bytes: int | None = None,
        allow_redirects: bool = False,
    ) -> SafeHttpResponse:
        current_url = url
        redirects = 0
        normalized_method = method.upper()
        if max_bytes < 0 or (max_error_bytes is not None and max_error_bytes < 0):
            raise ValueError("response byte limits must be non-negative")
        if allow_redirects and normalized_method not in {"GET", "HEAD"}:
            raise NetworkPolicyError("only GET and HEAD outbound requests may follow redirects")
        if self._exchange is not None:
            return self._request_via_exchange(
                normalized_method,
                url,
                headers=dict(headers or {}),
                content=content,
                max_bytes=max_bytes,
                max_error_bytes=max_error_bytes,
                allow_redirects=allow_redirects,
            )
        with self._client_factory() as client:
            while True:
                target = validate_https_url(current_url, resolver=self._resolver)
                with client.stream(normalized_method, current_url, headers=dict(headers or {}), content=content) as response:
                    try:
                        peer = ipaddress.ip_address(_address_from_sockaddr(self._peer_getter(response)))
                    except ValueError as exc:
                        raise NetworkPolicyError("outbound transport connected to an invalid peer") from exc
                    if not _is_public(peer) or peer not in target.addresses:
                        raise NetworkPolicyError("outbound transport connected to an unvalidated peer")
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not allow_redirects or not location:
                            raise NetworkPolicyError("outbound redirect is not allowed")
                        redirects += 1
                        if redirects > self._max_redirects:
                            raise NetworkPolicyError("outbound redirect limit exceeded")
                        current_url = urljoin(current_url, location)
                        continue
                    limit = max_error_bytes if response.status_code >= 400 and max_error_bytes is not None else max_bytes
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            if int(length) > limit:
                                raise ResponseLimitError("outbound response exceeded the configured byte limit")
                        except ValueError as exc:
                            raise NetworkPolicyError("outbound response has an invalid content length") from exc
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > limit:
                            raise ResponseLimitError("outbound response exceeded the configured byte limit")
                        body.extend(chunk)
                    return SafeHttpResponse(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=bytes(body),
                        url=current_url,
                        peer=peer,
                    )


    def _request_via_exchange(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None,
        max_bytes: int,
        max_error_bytes: int | None,
        allow_redirects: bool,
    ) -> SafeHttpResponse:
        current_url = url
        redirects = 0
        while True:
            target = validate_https_url(current_url, resolver=self._resolver)
            assert self._exchange is not None
            response, raw_peer = self._exchange(
                method,
                current_url,
                headers,
                content,
                (10.0, 30.0, 60.0),
            )
            try:
                peer = ipaddress.ip_address(_address_from_sockaddr(raw_peer))
            except ValueError as exc:
                raise NetworkPolicyError("outbound transport connected to an invalid peer") from exc
            if not _is_public(peer) or peer not in target.addresses:
                raise NetworkPolicyError("outbound transport connected to an unvalidated peer")
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not allow_redirects or not location:
                    raise NetworkPolicyError("outbound redirect is not allowed")
                redirects += 1
                if redirects > self._max_redirects:
                    raise NetworkPolicyError("outbound redirect limit exceeded")
                current_url = urljoin(current_url, location)
                continue
            limit = max_error_bytes if response.status_code >= 400 and max_error_bytes is not None else max_bytes
            if len(response.body) > limit:
                raise ResponseLimitError("outbound response exceeded the configured byte limit")
            return SafeHttpResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=response.body,
                url=current_url,
                peer=peer,
            )


class ArtifactFetchTransport:
    """Credential-free bounded artifact retrieval through the same safe transport."""

    def __init__(self, transport: SafeHttpTransport | None = None):
        self._transport = transport or SafeHttpTransport()

    def fetch(self, url: str, *, max_bytes: int = 100 * 1024 * 1024) -> SafeHttpResponse:
        return self._transport.request("GET", url, max_bytes=max_bytes, allow_redirects=True)

    def fetch_to(self, url: str, target: Any, *, max_bytes: int = 100 * 1024 * 1024) -> ArtifactFetchResult:
        import hashlib

        response = self.fetch(url, max_bytes=max_bytes)
        target.write(response.body)
        return ArtifactFetchResult(size=len(response.body), sha256=hashlib.sha256(response.body).hexdigest())
