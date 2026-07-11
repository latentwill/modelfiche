from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from titles_api.storage.contracts import ContentVariantV1, ThumbnailVariantV1
from titles_api.storage.delivery import DeliveryTokenError, DeliveryTokenLedger


def test_proxy_capability_is_bound_to_its_asset_revision_and_variant():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    asset_id = uuid4()
    revision_id = uuid4()
    ledger = DeliveryTokenLedger(clock=lambda: now)
    capability = ledger.issue_capability(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        location_id=uuid4(),
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
        expires_at=now + timedelta(minutes=5),
        payload="opaque reader",
    )

    with pytest.raises(DeliveryTokenError, match="revision"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=uuid4(),
            variant=ContentVariantV1(kind="content"),
            host="titles.local",
        )
    with pytest.raises(DeliveryTokenError, match="variant"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="titles.local",
        )

    assert ledger.resolve_capability(
        capability.token,
        asset_id=asset_id,
        asset_revision_id=revision_id,
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
    ).payload == "opaque reader"


def test_alternate_token_is_host_bound_short_lived_and_single_use():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    asset_id = uuid4()
    revision_id = uuid4()
    ledger = DeliveryTokenLedger(clock=lambda: now)
    alternate = ledger.issue_alternate(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        alternate_location_id=uuid4(),
        variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
        host="titles.local",
    )

    with pytest.raises(DeliveryTokenError, match="host"):
        ledger.consume_alternate(
            alternate.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="other.local",
        )

    assert ledger.consume_alternate(
        alternate.token,
        asset_id=asset_id,
        asset_revision_id=revision_id,
        variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
        host="titles.local",
    ).alternate_location_id == alternate.alternate_location_id
    with pytest.raises(DeliveryTokenError, match="unknown"):
        ledger.consume_alternate(
            alternate.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ThumbnailVariantV1(kind="thumbnail", max_pixels=512),
            host="titles.local",
        )


def test_expired_capability_is_not_resolved():
    now = datetime(2026, 7, 11, tzinfo=UTC)
    ledger = DeliveryTokenLedger(clock=lambda: now)
    asset_id = uuid4()
    revision_id = uuid4()
    capability = ledger.issue_capability(
        asset_id=asset_id,
        asset_revision_id=revision_id,
        location_id=uuid4(),
        variant=ContentVariantV1(kind="content"),
        host="titles.local",
        expires_at=now + timedelta(seconds=1),
        payload=None,
    )

    now += timedelta(seconds=2)

    with pytest.raises(DeliveryTokenError, match="expired"):
        ledger.resolve_capability(
            capability.token,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            variant=ContentVariantV1(kind="content"),
            host="titles.local",
        )
