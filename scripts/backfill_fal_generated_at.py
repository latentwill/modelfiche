#!/usr/bin/env python3
"""Backfill authoritative FAL completion time onto historical eval assets."""

from sqlalchemy import select

from titles_api import models
from titles_api.database import SessionLocal
from titles_api.image_provenance import fal_generated_at


def main() -> None:
    updated = 0
    with SessionLocal.begin() as session:
        outputs = session.scalars(select(models.EvalOutput).order_by(models.EvalOutput.id)).all()
        for output in outputs:
            metadata = dict(output.provider_metadata or {})
            response = metadata.get("response") if isinstance(metadata.get("response"), dict) else {}
            generated_at = fal_generated_at(response)
            if generated_at is None:
                continue
            asset = session.get(models.Asset, output.asset_id)
            if asset is None or str((asset.metadata_ or {}).get("provider", "")).lower() != "fal":
                continue
            asset.metadata_ = {**dict(asset.metadata_ or {}), "generated_at": generated_at.isoformat()}
            asset.created_at = generated_at
            output.created_at = generated_at
            updated += 1
    print({"updated": updated})


if __name__ == "__main__":
    main()
