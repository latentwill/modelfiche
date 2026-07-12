from __future__ import annotations

from typing import Protocol

from titles_api.storage.network import SafeHttpTransport


class ValidationTransport(Protocol):
    def request(self, method: str, url: str, **kwargs): ...


def validate_fal_connection(key: str, transport: ValidationTransport | None = None) -> None:
    """Authenticate against FAL's model catalog without submitting billable work."""
    client = transport or SafeHttpTransport()
    response = client.request(
        "GET",
        "https://api.fal.ai/v1/models?limit=1",
        headers={"Authorization": f"Key {key}", "Accept": "application/json"},
        max_bytes=256 * 1024,
        max_error_bytes=64 * 1024,
        allow_redirects=False,
    )
    if response.status_code in {401, 403}:
        raise ValueError("FAL rejected the credential; verify the key and its permissions")
    if response.status_code >= 400:
        raise RuntimeError(f"FAL validation returned HTTP {response.status_code}; try again or check provider status")
