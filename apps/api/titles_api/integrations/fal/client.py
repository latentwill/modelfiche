from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

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


class FalQueueClient:
    """Minimal FAL queue API client; it never logs headers or signed URLs."""

    def __init__(self, settings: FalSettings, *, timeout: float = 30.0):
        self.settings = settings
        self.timeout = timeout

    def submit(self, endpoint_id: str, payload: dict[str, Any]) -> Submission:
        result = self._request("POST", f"{self.settings.base_url}/{endpoint_id}", payload)
        request_id = result.get("request_id")
        if not request_id:
            raise FalProviderError("FAL response did not contain a request_id")
        root = f"{self.settings.base_url}/{endpoint_id}/requests/{request_id}"
        return Submission(
            request_id=request_id,
            status_url=result.get("status_url", f"{root}/status"),
            response_url=result.get("response_url", root),
            cancel_url=result.get("cancel_url", f"{root}/cancel"),
        )

    def status(self, status_url: str) -> dict[str, Any]:
        return self._request("GET", status_url)

    def result(self, response_url: str) -> dict[str, Any]:
        return self._request("GET", response_url)

    def cancel(self, cancel_url: str) -> dict[str, Any]:
        return self._request("PUT", cancel_url)

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Authorization": f"Key {self.settings.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", "replace")
            try:
                message = json.loads(detail).get("detail", detail)
            except json.JSONDecodeError:
                message = detail
            raise FalProviderError(str(message), status=exc.code, retryable=exc.code == 429 or exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise FalProviderError("FAL request failed", retryable=True) from exc
