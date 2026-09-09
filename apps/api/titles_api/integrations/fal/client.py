from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from titles_api.storage.network import JsonShapeError, NetworkPolicyError, ResponseLimitError, SafeHttpTransport, decode_bounded_json

from ..config import FalSettings


class FalProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Submission:
    request_id: str
    status_url: str
    response_url: str
    cancel_url: str | None = None


_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")


class FalApiTransport:
    """Bounded FAL API transport that never follows provider-supplied lifecycle URLs."""

    def __init__(self, settings: FalSettings, *, transport: SafeHttpTransport | None = None):
        self._api_key = settings.api_key
        self._origin = self._validate_base_url(settings.base_url)
        self._transport = transport or SafeHttpTransport()

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise FalProviderError("FAL base URL must be an HTTPS API origin")
        return f"https://{parsed.netloc}"

    @staticmethod
    def _endpoint_id(endpoint_id: str) -> str:
        if not _ENDPOINT_ID.fullmatch(endpoint_id):
            raise FalProviderError("FAL endpoint identifier is invalid")
        return endpoint_id

    @staticmethod
    def _request_id(request_id: str) -> str:
        if not _REQUEST_ID.fullmatch(request_id):
            raise FalProviderError("FAL request identifier is invalid")
        return request_id

    def submission_url(self, endpoint_id: str) -> str:
        return f"{self._origin}/{self._endpoint_id(endpoint_id)}"

    def lifecycle_url(self, endpoint_id: str, request_id: str, operation: str) -> str:
        endpoint = self._endpoint_id(endpoint_id)
        request = self._request_id(request_id)
        if operation not in {"status", "result", "cancel"}:
            raise FalProviderError("FAL lifecycle operation is invalid")
        suffix = {"status": "/status", "result": "", "cancel": "/cancel"}[operation]
        return f"{self._origin}/{endpoint}/requests/{request}{suffix}"

    def require_canonical_url(self, url: str) -> str:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme.lower()}://{parsed.netloc}"
        if (
            origin != self._origin
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise FalProviderError("FAL lifecycle URL is outside the canonical API origin")
        return url

    def require_lifecycle_url(self, url: Any, request_id: str, operation: str) -> str:
        if not isinstance(url, str):
            raise FalProviderError(f"FAL response did not contain a valid {operation}_url")
        canonical = self.require_canonical_url(url)
        parsed = urlsplit(canonical)
        marker = f"/requests/{self._request_id(request_id)}"
        if marker not in parsed.path:
            raise FalProviderError("FAL lifecycle URL does not match the submitted request")
        endpoint, suffix = parsed.path.split(marker, 1)
        self._endpoint_id(endpoint.lstrip("/"))
        allowed = {
            "status": {"/status"},
            "response": {"", "/response"},
            "cancel": {"/cancel"},
        }
        if operation not in allowed or suffix not in allowed[operation]:
            raise FalProviderError("FAL lifecycle URL has an invalid operation path")
        return canonical

    def request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        canonical = self.require_canonical_url(url)
        content: bytes | None = None
        if payload is not None:
            try:
                content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise FalProviderError("FAL request payload is not JSON serializable") from exc
            if len(content) > 2 * 1024 * 1024:
                raise FalProviderError("FAL request payload exceeds the configured byte limit")
        try:
            response = self._transport.request(
                method,
                canonical,
                headers={"Authorization": f"Key {self._api_key}", "Content-Type": "application/json"},
                content=content,
                max_bytes=2 * 1024 * 1024,
                max_error_bytes=64 * 1024,
                allow_redirects=False,
            )
        except ResponseLimitError as exc:
            raise FalProviderError("FAL response exceeded the configured byte limit", retryable=False) from exc
        except NetworkPolicyError as exc:
            raise FalProviderError("FAL transport rejected the request", retryable=False) from exc
        try:
            result = decode_bounded_json(response.body)
        except (JsonShapeError, ResponseLimitError) as exc:
            raise FalProviderError("FAL response has an invalid shape", status=response.status_code, retryable=False) from exc
        return response.status_code, result


class FalQueueClient:
    """Queue client with a credentialed canonical-origin API transport only."""

    def __init__(self, settings: FalSettings, *, timeout: float = 30.0, transport: FalApiTransport | None = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.settings = settings
        self.timeout = timeout
        self._transport = transport or FalApiTransport(settings)

    def submit(self, endpoint_id: str, payload: dict[str, Any]) -> Submission:
        status, result = self._request("POST", self._transport.submission_url(endpoint_id), payload)
        if status >= 400:
            raise self._error_for_status(status)
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise FalProviderError("FAL response did not contain a valid request_id", status=status)
        self._transport._request_id(request_id)
        return Submission(
            request_id=request_id,
            status_url=self._transport.require_lifecycle_url(result.get("status_url"), request_id, "status"),
            response_url=self._transport.require_lifecycle_url(result.get("response_url"), request_id, "response"),
            cancel_url=self._transport.require_lifecycle_url(result.get("cancel_url"), request_id, "cancel"),
        )
    def resume_submission(self, endpoint_id: str, request_id: str) -> Submission:
        """Rebuild canonical lifecycle URLs for a previously persisted request."""
        self._transport._request_id(request_id)
        return Submission(
            request_id=request_id,
            status_url=self._transport.lifecycle_url(endpoint_id, request_id, "status"),
            response_url=self._transport.lifecycle_url(endpoint_id, request_id, "result"),
            cancel_url=self._transport.lifecycle_url(endpoint_id, request_id, "cancel"),
        )


    def status(self, status_url: str) -> dict[str, Any]:
        status, result = self._request("GET", status_url)
        if status >= 400:
            raise self._error_for_status(status)
        return result

    def result(self, response_url: str) -> dict[str, Any]:
        status, result = self._request("GET", response_url)
        if status >= 400:
            raise self._error_for_status(status)
        return result

    def cancel(self, cancel_url: str) -> dict[str, Any]:
        status, result = self._request("PUT", cancel_url)
        if status >= 400:
            raise self._error_for_status(status)
        return result

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        return self._transport.request(method, url, payload)

    @staticmethod
    def _error_for_status(status: int) -> FalProviderError:
        return FalProviderError(
            "FAL provider rejected the request",
            status=status,
            retryable=status == 429 or status >= 500,
        )


__all__ = ["FalApiTransport", "FalProviderError", "FalQueueClient", "Submission"]
