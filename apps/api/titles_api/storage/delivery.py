from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import secrets
from typing import Any
from uuid import UUID

from .contracts import AssetDeliveryVariantV1


class DeliveryTokenError(RuntimeError):
    """An opaque delivery token was expired, replayed, or used out of context."""


@dataclass(frozen=True, slots=True)
class DeliveryBinding:
    asset_id: UUID
    asset_revision_id: UUID
    location_id: UUID
    variant: AssetDeliveryVariantV1
    host: str
    expires_at: datetime
    descriptor_version: int = 1

@dataclass(frozen=True, slots=True)
class DeliveryCapability:
    token: str
    binding: DeliveryBinding
    payload: Any

    @property
    def asset_id(self) -> UUID:
        return self.binding.asset_id

    @property
    def asset_revision_id(self) -> UUID:
        return self.binding.asset_revision_id

    @property
    def location_id(self) -> UUID:
        return self.binding.location_id

    @property
    def variant(self) -> AssetDeliveryVariantV1:
        return self.binding.variant

    @property
    def host(self) -> str:
        return self.binding.host

    @property
    def expires_at(self) -> datetime:
        return self.binding.expires_at


@dataclass(frozen=True, slots=True)
class AlternateCapability:
    token: str
    binding: DeliveryBinding
    alternate_location_id: UUID

    @property
    def asset_id(self) -> UUID:
        return self.binding.asset_id

    @property
    def asset_revision_id(self) -> UUID:
        return self.binding.asset_revision_id

    @property
    def variant(self) -> AssetDeliveryVariantV1:
        return self.binding.variant

    @property
    def host(self) -> str:
        return self.binding.host

    @property
    def expires_at(self) -> datetime:
        return self.binding.expires_at


class DeliveryTokenLedger:
    """Process-local capability ledger for proxy, alternate, and diagnosis tokens.

    Capabilities are deliberately not serializable. A server restart discards every
    token, which makes a stale URL or alternate token fail closed.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None, boot_id: str | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self.boot_id = boot_id or _token()
        self._capabilities: dict[str, DeliveryCapability] = {}
        self._alternates: dict[str, AlternateCapability] = {}
        self._diagnostics: dict[str, DeliveryCapability] = {}
    def issue_capability(
        self,
        *,
        asset_id: UUID,
        asset_revision_id: UUID,
        location_id: UUID,
        variant: AssetDeliveryVariantV1,
        host: str,
        expires_at: datetime,
        payload: Any,
        descriptor_version: int = 1,
    ) -> DeliveryCapability:
        capability = DeliveryCapability(
            token=_token(),
            binding=_binding(
                asset_id,
                asset_revision_id,
                location_id,
                variant,
                host,
                expires_at,
                descriptor_version,
            ),
            payload=payload,
        )
        self._capabilities[capability.token] = capability
        return capability

    def resolve_capability(
        self,
        token: str,
        *,
        host: str,
        asset_id: UUID | None = None,
        asset_revision_id: UUID | None = None,
        variant: AssetDeliveryVariantV1 | None = None,
        descriptor_version: int | None = None,
    ) -> DeliveryCapability:
        capability = self._get(self._capabilities, token)
        _assert_binding(capability.binding, asset_id, asset_revision_id, variant, host, descriptor_version)
        return capability

    def issue_alternate(
        self,
        *,
        asset_id: UUID,
        asset_revision_id: UUID,
        alternate_location_id: UUID,
        variant: AssetDeliveryVariantV1,
        host: str,
        descriptor_version: int = 1,
        expires_at: datetime | None = None,
    ) -> AlternateCapability:
        alternate = AlternateCapability(
            token=_token(),
            binding=_binding(
                asset_id,
                asset_revision_id,
                alternate_location_id,
                variant,
                host,
                expires_at or (self._now() + timedelta(seconds=60)),
                descriptor_version,
            ),
            alternate_location_id=alternate_location_id,
        )
        self._alternates[alternate.token] = alternate
        return alternate

    def consume_alternate(
        self,
        token: str,
        *,
        asset_id: UUID,
        asset_revision_id: UUID,
        variant: AssetDeliveryVariantV1,
        host: str,
        descriptor_version: int | None = None,
    ) -> AlternateCapability:
        alternate = self._get(self._alternates, token)
        _assert_binding(alternate.binding, asset_id, asset_revision_id, variant, host, descriptor_version)
        del self._alternates[token]
        return alternate

    def issue_diagnostic(
        self,
        *,
        asset_id: UUID,
        asset_revision_id: UUID,
        location_id: UUID,
        variant: AssetDeliveryVariantV1,
        host: str,
        expires_at: datetime,
        payload: Any,
        descriptor_version: int = 1,
    ) -> DeliveryCapability:
        diagnostic = DeliveryCapability(
            token=_token(),
            binding=_binding(
                asset_id,
                asset_revision_id,
                location_id,
                variant,
                host,
                expires_at,
                descriptor_version,
            ),
            payload=payload,
        )
        self._diagnostics[diagnostic.token] = diagnostic
        return diagnostic

    def resolve_diagnostic(
        self,
        token: str,
        *,
        host: str,
        asset_id: UUID | None = None,
        asset_revision_id: UUID | None = None,
        variant: AssetDeliveryVariantV1 | None = None,
        descriptor_version: int | None = None,
    ) -> DeliveryCapability:
        diagnostic = self._get(self._diagnostics, token)
        _assert_binding(diagnostic.binding, asset_id, asset_revision_id, variant, host, descriptor_version)
        return diagnostic
    def _get(self, entries: dict[str, Any], token: str) -> Any:
        entry = entries.get(token)
        if entry is None:
            raise DeliveryTokenError("delivery token is unknown")
        if self._now() >= entry.binding.expires_at:
            del entries[token]
            raise DeliveryTokenError("delivery token is expired")
        return entry

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("delivery ledger clock must return an aware datetime")
        return now


def _binding(
    asset_id: UUID,
    asset_revision_id: UUID,
    location_id: UUID,
    variant: AssetDeliveryVariantV1,
    host: str,
    expires_at: datetime,
    descriptor_version: int = 1,
) -> DeliveryBinding:
    if not host:
        raise ValueError("delivery host is required")
    if expires_at.tzinfo is None:
        raise ValueError("delivery expiry must be timezone-aware")
    if descriptor_version != 1:
        raise ValueError("unsupported delivery descriptor version")
    return DeliveryBinding(
        asset_id=asset_id,
        asset_revision_id=asset_revision_id,
        location_id=location_id,
        variant=variant,
        host=host,
        expires_at=expires_at,
        descriptor_version=descriptor_version,
    )
def _assert_binding(
    binding: DeliveryBinding,
    asset_id: UUID | None,
    asset_revision_id: UUID | None,
    variant: AssetDeliveryVariantV1 | None,
    host: str,
    descriptor_version: int | None = None,
) -> None:
    if asset_id is not None and binding.asset_id != asset_id:
        raise DeliveryTokenError("delivery token asset does not match")
    if asset_revision_id is not None and binding.asset_revision_id != asset_revision_id:
        raise DeliveryTokenError("delivery token revision does not match")
    if variant is not None and binding.variant != variant:
        raise DeliveryTokenError("delivery token variant does not match")
    if descriptor_version is not None and binding.descriptor_version != descriptor_version:
        raise DeliveryTokenError("delivery token descriptor version does not match")
    if binding.host != host:
        raise DeliveryTokenError("delivery token host does not match")


def _token() -> str:
    return secrets.token_urlsafe(32)
