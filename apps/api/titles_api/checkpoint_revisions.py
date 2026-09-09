from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import models


def best_verified_location(db: Session, asset_id: str) -> models.AssetLocation | None:
    return db.scalar(
        select(models.AssetLocation)
        .where(
            models.AssetLocation.asset_id == asset_id,
            models.AssetLocation.verification_state.in_(("available", "verified")),
        )
        .order_by(
            case((models.AssetLocation.provider == "local", 0), else_=1),
            models.AssetLocation.last_verified_at.desc(),
            models.AssetLocation.created_at.desc(),
        )
    )


def establish_checkpoint_revision(
    db: Session,
    checkpoint: models.Checkpoint,
    *,
    location: models.AssetLocation | None = None,
    force_new: bool = False,
) -> models.CheckpointRevision | None:
    location = location or best_verified_location(db, checkpoint.asset_id)
    if location is None:
        return None
    current = db.get(models.CheckpointRevision, checkpoint.current_revision_id) if checkpoint.current_revision_id else None
    if current and not force_new:
        return current
    if current and current.source_location_id == location.id:
        return current
    asset = db.get(models.Asset, checkpoint.asset_id)
    ordinal = int(db.scalar(select(func.max(models.CheckpointRevision.ordinal)).where(models.CheckpointRevision.checkpoint_id == checkpoint.id)) or 0) + 1
    revision = models.CheckpointRevision(
        checkpoint_id=checkpoint.id,
        ordinal=ordinal,
        asset_id=checkpoint.asset_id,
        content_blob_id=asset.content_blob_id if asset else None,
        source_location_id=location.id,
        supersedes_id=current.id if current else None,
    )
    db.add(revision)
    db.flush()
    checkpoint.current_revision_id = revision.id
    for version in db.scalars(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id)):
        version.checkpoint_revision_id = revision.id
    return revision
