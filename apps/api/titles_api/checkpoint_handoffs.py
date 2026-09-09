from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .training_launches import CHECKPOINT_MANIFEST_VERSION
from .training_metrics import append_run_event
from .wandb_auth import WandbPrincipal


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HandoffError(ValueError):
    pass


class HandoffConflict(HandoffError):
    pass


def _required_string(body: dict[str, Any], name: str, *, maximum: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise HandoffError(f"checkpoint handoff requires a valid {name}")
    return value


def record_checkpoint_handoff(
    session: Session,
    principal: WandbPrincipal,
    run_id: str,
    body: object,
) -> tuple[models.RunArtifactHandoff, bool]:
    if run_id != principal.run.id:
        raise HandoffError("checkpoint handoff run does not match the launch token")
    if not isinstance(body, dict):
        raise HandoffError("checkpoint handoff body must be an object")
    if body.get("schema_version") != CHECKPOINT_MANIFEST_VERSION:
        raise HandoffError("unsupported checkpoint manifest schema version")
    generation = body.get("generation")
    checkpoint_count = body.get("checkpoint_count")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise HandoffError("checkpoint handoff generation must be a positive integer")
    if not isinstance(checkpoint_count, int) or isinstance(checkpoint_count, bool) or checkpoint_count < 0:
        raise HandoffError("checkpoint_count must be a non-negative integer")
    manifest_key = _required_string(body, "manifest_key", maximum=1024)
    expected_key = f"{principal.launch.run_prefix}manifests/checkpoints/{generation}.json"
    if manifest_key != expected_key:
        raise HandoffError("checkpoint manifest key is outside its generation-fenced run prefix")
    manifest_digest = _required_string(body, "manifest_sha256", maximum=64).casefold()
    if _SHA256.fullmatch(manifest_digest) is None:
        raise HandoffError("checkpoint manifest SHA-256 is invalid")
    manifest_etag = body.get("manifest_etag")
    manifest_version_id = body.get("manifest_version_id")
    if manifest_etag is not None and (not isinstance(manifest_etag, str) or not manifest_etag or len(manifest_etag) > 255):
        raise HandoffError("checkpoint manifest ETag is invalid")
    if manifest_version_id is not None and (
        not isinstance(manifest_version_id, str) or not manifest_version_id or len(manifest_version_id) > 1024
    ):
        raise HandoffError("checkpoint manifest version ID is invalid")
    if manifest_etag is None and manifest_version_id is None:
        raise HandoffError("checkpoint handoff requires an ETag or version ID")

    existing = session.scalar(
        select(models.RunArtifactHandoff).where(
            models.RunArtifactHandoff.run_id == run_id,
            models.RunArtifactHandoff.generation == generation,
        )
    )
    if existing is not None:
        same = (
            existing.manifest_key == manifest_key
            and existing.manifest_digest == manifest_digest
            and existing.manifest_etag == manifest_etag
            and existing.manifest_version_id == manifest_version_id
        )
        if not same:
            raise HandoffConflict("checkpoint handoff generation is already bound to another manifest")
        return existing, False

    source_value = (
        f"{principal.launch.source_id or ''}\0{manifest_key}\0"
        f"{manifest_version_id or manifest_etag or ''}\0{manifest_digest}"
    )
    now = models.utcnow()
    handoff = models.RunArtifactHandoff(
        run_id=run_id,
        generation=generation,
        manifest_key=manifest_key,
        manifest_etag=manifest_etag,
        manifest_version_id=manifest_version_id,
        manifest_digest=manifest_digest,
        state="observed",
        source_fingerprint=hashlib.sha256(source_value.encode("utf-8")).hexdigest(),
        checkpoint_count=checkpoint_count,
        observed_at=now,
    )
    session.add(handoff)
    session.flush()
    principal.launch.backup_status = "reconciling"
    principal.launch.checkpoint_handoff_status = "observed"
    principal.launch.next_reconcile_at = now
    append_run_event(
        session,
        principal.run,
        type="run.checkpoint_manifest.observed",
        idempotency_key=f"checkpoint-manifest:{run_id}:{generation}:{manifest_digest}",
        payload={
            "handoff_id": handoff.id,
            "generation": generation,
            "manifest_key": manifest_key,
            "checkpoint_count": checkpoint_count,
        },
        step=principal.run.current_step,
        occurred_at=now,
    )
    return handoff, True


def handoff_projection(handoff: models.RunArtifactHandoff) -> dict[str, object]:
    return {
        "id": handoff.id,
        "run_id": handoff.run_id,
        "generation": handoff.generation,
        "manifest_key": handoff.manifest_key,
        "state": handoff.state,
        "checkpoint_count": handoff.checkpoint_count,
        "verified_count": handoff.verified_count,
        "imported_count": handoff.imported_count,
        "failed_count": handoff.failed_count,
        "error": handoff.error,
        "observed_at": handoff.observed_at,
        "verified_at": handoff.verified_at,
        "imported_at": handoff.imported_at,
    }


__all__ = [
    "HandoffConflict",
    "HandoffError",
    "handoff_projection",
    "record_checkpoint_handoff",
]
