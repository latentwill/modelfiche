from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..integrations.config import S3Settings
from ..integrations.fal.adapters import LoraInput, ValidationError, get_adapter, list_adapters
from ..integrations.fal.credentials import fal_secret_store, resolve_fal_credential
from ..integrations.s3.browser import PrefixAccessError, S3Browser, normalize_prefix
from ..integrations.s3.detection import detect_prefix
from ..storage.identity import SourceIdentityError, normalize_source_identity
from ..review_tokens import issue_review_token, verify_review_token


def _persist_source_identity(source: models.ImportSource) -> None:
    """Store a canonical source revision whenever configuration is usable for training."""
    if not source.allowed_prefixes:
        source.identity_fingerprint = None
        return
    managed_prefix = source.managed_prefix or source.allowed_prefixes[0]
    try:
        identity = normalize_source_identity(
            endpoint_url=source.endpoint_url,
            bucket=source.bucket,
            region=source.region,
            addressing_style=source.addressing_style,
            credential_env_prefix=source.credential_env_prefix,
            allowed_prefixes=source.allowed_prefixes,
            managed_prefix=managed_prefix,
        )
    except SourceIdentityError:
        # Legacy sources used a global default managed prefix that was often
        # outside their restricted allowlist. Their first allowlisted prefix is
        # the narrowest safe managed scope until an operator configures one.
        identity = normalize_source_identity(
            endpoint_url=source.endpoint_url,
            bucket=source.bucket,
            region=source.region,
            addressing_style=source.addressing_style,
            credential_env_prefix=source.credential_env_prefix,
            allowed_prefixes=source.allowed_prefixes,
            managed_prefix=source.allowed_prefixes[0],
        )
    source.managed_prefix = identity.managed_prefix
    source.identity_fingerprint = identity.fingerprint

def _detection_scope(source_id: str, prefix: str, result: Any) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "prefix": normalize_prefix(prefix),
        "kind": result.kind.value,
        "observed": result.observed,
        "declared": result.declared,
        "metadata": result.metadata,
    }

def _detect_for_review(source_id: str, prefix: str, db: Session) -> tuple[Any, dict[str, Any]]:
    browser = _browser(db, source_id)
    try:
        result = detect_prefix(browser.inventory(prefix), browser.read_small)
    except PrefixAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return result, _detection_scope(source_id, prefix, result)
from ..services import active_profile, current_workspace

router = APIRouter(tags=["integrations"])


class PrefixRequest(BaseModel):
    prefix: str = ""


class ImportSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    endpoint_url: str | None = None
    region: str | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    credential_env_prefix: str = Field(default="S3", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    bucket: str = Field(min_length=1, max_length=255)
    allowed_prefixes: list[str] = Field(default_factory=list)


class ImportSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    endpoint_url: str | None = None
    region: str | None = None
    addressing_style: Literal["auto", "path", "virtual"] | None = None
    credential_env_prefix: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    bucket: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_prefixes: list[str] | None = None
    is_active: bool | None = None


class ImportRequest(PrefixRequest):
    source_id: str
    project_id: str
    profile_id: str | None = None
    kind: Literal["dataset", "training_run"] | None = None
    hydrate_dataset_images: bool = True
    dry_run: bool = False
    idempotency_key: str | None = None
    review_token: str | None = None

class FalValidateRequest(BaseModel):
    prompt: str
    loras: list[dict[str, Any]] = []
    parameters: dict[str, Any] = {}
    grid_cell: bool = False


@router.get("/api/import-sources")
def list_import_sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    workspace = current_workspace(db)
    sources = db.scalars(
        select(models.ImportSource)
        .where(models.ImportSource.workspace_id == workspace.id, models.ImportSource.is_active.is_(True))
        .order_by(models.ImportSource.name)
    )
    return [
        {
            "id": source.id,
            "name": source.name,
            "provider": source.provider,
            "endpoint_url": source.endpoint_url,
            "region": source.region,
            "addressing_style": source.addressing_style,
            "credential_env_prefix": source.credential_env_prefix,
            "bucket": source.bucket,
            "allowed_prefixes": source.allowed_prefixes,
            "is_active": source.is_active,
        }
        for source in sources
    ]


@router.get("/api/connections")
def list_connections(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    source = db.scalar(select(models.ImportSource).where(models.ImportSource.is_active.is_(True)).order_by(models.ImportSource.created_at))
    return [
        {"provider": "s3", "configured": source is not None, "source_id": source.id if source else None, "bucket": source.bucket if source else None},
        {"provider": "fal", "configured": resolve_fal_credential() is not None},
    ]


@router.post("/api/connections/{provider}/test")
def test_connection(provider: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider == "fal":
        configured = resolve_fal_credential() is not None
        if not configured:
            raise HTTPException(status_code=409, detail="FAL_KEY or FAL_API_KEY is not configured")
        return {"provider": "fal", "ok": True, "network_tested": False}
    if provider in {"s3", "mega"}:
        query = select(models.ImportSource).where(models.ImportSource.is_active.is_(True))
        query = query.where(models.ImportSource.credential_env_prefix == "MEGA") if provider == "mega" else query.where(models.ImportSource.credential_env_prefix != "MEGA")
        source = db.scalar(query.order_by(models.ImportSource.created_at))
        if source is None:
            raise HTTPException(status_code=409, detail=f"no active {provider.upper()} import source is configured")
        return {"provider": provider, **test_import_source(source.id, db)}
    raise HTTPException(status_code=404, detail="unknown connection provider")


@router.post("/api/import-sources", status_code=201)
def create_import_source(request: ImportSourceCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = current_workspace(db)
    try:
        normalized = [normalize_prefix(prefix) for prefix in request.allowed_prefixes]
    except PrefixAccessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source = models.ImportSource(
        workspace_id=workspace.id,
        name=request.name,
        provider="s3",
        endpoint_url=request.endpoint_url.rstrip("/") if request.endpoint_url else None,
        region=request.region,
        addressing_style=request.addressing_style,
        credential_env_prefix=request.credential_env_prefix.upper(),
        bucket=request.bucket,
        allowed_prefixes=normalized,
    )
    _persist_source_identity(source)
    db.add(source)
    db.commit()
    db.refresh(source)
    return {"id": source.id, "name": source.name, "bucket": source.bucket, "allowed_prefixes": source.allowed_prefixes}


@router.patch("/api/import-sources/{source_id}")
def update_import_source(source_id: str, request: ImportSourceUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = current_workspace(db)
    source = db.scalar(
        select(models.ImportSource).where(
            models.ImportSource.id == source_id,
            models.ImportSource.workspace_id == workspace.id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="import source not found")
    values = request.model_dump(exclude_unset=True)
    if "allowed_prefixes" in values:
        try:
            values["allowed_prefixes"] = [normalize_prefix(prefix) for prefix in values["allowed_prefixes"]]
        except PrefixAccessError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if values.get("endpoint_url"):
        values["endpoint_url"] = values["endpoint_url"].rstrip("/")
    if values.get("credential_env_prefix"):
        values["credential_env_prefix"] = values["credential_env_prefix"].upper()
    for key, value in values.items():
        setattr(source, key, value)
    _persist_source_identity(source)
    db.commit()
    return {"id": source.id, "name": source.name, "bucket": source.bucket, "allowed_prefixes": source.allowed_prefixes}


@router.delete("/api/import-sources/{source_id}", status_code=204)
def disconnect_import_source(source_id: str, db: Session = Depends(get_db)) -> None:
    workspace = current_workspace(db)
    source = db.scalar(
        select(models.ImportSource).where(
            models.ImportSource.id == source_id,
            models.ImportSource.workspace_id == workspace.id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="import source not found")
    source.is_active = False
    db.commit()


@router.post("/api/import-sources/{source_id}/test")
def test_import_source(source_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    browser = _browser(db, source_id)
    try:
        first_root = browser.settings.allowed_prefixes[0] if browser.settings.allowed_prefixes else ""
        browser.browse(first_root, page_size=1)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"S3 connection failed: {exc}") from exc
    source = _source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="import source not found")
    _persist_source_identity(source)
    db.commit()
    return {"ok": True, "bucket": browser.settings.bucket, "allowed_prefixes": browser.settings.allowed_prefixes}


@router.get("/api/import-sources/{source_id}/browse")
def browse_source(
    source_id: str,
    prefix: str = Query(default=""),
    cursor: str | None = None,
    page_size: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    browser = _browser(db, source_id)
    try:
        page = browser.browse(prefix, cursor=cursor, page_size=page_size)
    except PrefixAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "prefix": page.prefix,
        "prefixes": list(page.prefixes),
        "objects": [asdict(item) for item in page.objects],
        "next_cursor": page.next_cursor,
        "is_truncated": page.is_truncated,
    }


@router.post("/api/import-sources/{source_id}/detect")
def detect_source(source_id: str, request: PrefixRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result, scope = _detect_for_review(source_id, request.prefix, db)
    return {
        "kind": result.kind.value,
        "confidence": result.confidence,
        "signals": result.signals,
        "warnings": result.warnings,
        "observed": result.observed,
        "declared": result.declared,
        "metadata": result.metadata,
        "review_token": issue_review_token("s3-import", scope),
        "review_scope": scope,
    }


@router.post("/api/import-jobs", status_code=202)
def enqueue_import(
    request: ImportRequest,
    db: Session = Depends(get_db),
    x_profile_id: str | None = Header(default=None, alias="X-Profile-ID"),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    source = _source(db, request.source_id)
    project = db.get(models.Project, request.project_id)
    if not source or not project:
        raise HTTPException(status_code=404, detail="import source or project not found")
    if not request.dry_run:
        if not request.review_token:
            raise HTTPException(status_code=428, detail="S3 detection preview review_token is required before import")
        _result, scope = _detect_for_review(request.source_id, request.prefix, db)
        valid, reason = verify_review_token(request.review_token, "s3-import", scope)
        if not valid:
            raise HTTPException(status_code=409, detail=reason)
    profile = active_profile(db, request.profile_id or x_profile_id)
    idempotency_key = request.idempotency_key or idempotency_key_header
    payload = request.model_dump(exclude={"idempotency_key"}) | {"profile_id": profile.id}
    job = models.Job(
        workspace_id=source.workspace_id,
        kind="s3.import",
        profile_id=profile.id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if not idempotency_key:
            raise
        existing = db.scalar(select(models.Job).where(models.Job.idempotency_key == idempotency_key))
        if not existing:
            raise
        return {"job_id": existing.id, "state": existing.state.value, "idempotent_replay": True}
    import_job = models.ImportJob(
        source_id=source.id,
        project_id=project.id,
        job_id=job.id,
        prefix=request.prefix,
        state="queued",
    )
    db.add(import_job)
    db.commit()
    return {"id": import_job.id, "job_id": job.id, "state": "queued"}


@router.get("/api/providers/fal/endpoints")
def fal_endpoints() -> list[dict[str, Any]]:
    return [
        {
            "endpoint_id": adapter.endpoint_id,
            "defaults": adapter.defaults,
            "grid_axes": sorted(adapter.allowed_axes),
            "configured": resolve_fal_credential() is not None,
        }
        for adapter in list_adapters()
    ]


@router.get("/api/providers/fal/test")
def test_fal_configuration() -> dict[str, Any]:
    """Non-billable check; intentionally does not submit or enqueue a generation."""
    credential = resolve_fal_credential()
    if not credential:
        return {"ok": False, "configured": False, "validation": "missing"}
    return {
        "ok": True,
        "configured": True,
        "source": credential.source,
        "validation": (fal_secret_store().status().get("last_validation") or {}).get("state", "configuration_only"),
        "endpoints": [adapter.endpoint_id for adapter in list_adapters()],
    }


@router.post("/api/providers/fal/endpoints/{endpoint_id:path}/validate")
def validate_fal_request(endpoint_id: str, request: FalValidateRequest) -> dict[str, Any]:
    try:
        adapter = get_adapter(endpoint_id)
        payload = adapter.build_request(
            prompt=request.prompt,
            loras=[LoraInput(path=str(item.get("path", "")), scale=float(item.get("scale", 1))) for item in request.loras],
            parameters=request.parameters,
            grid_cell=request.grid_cell,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"endpoint_id": endpoint_id, "request": payload}


def _browser(db: Session, source_id: str) -> S3Browser:
    source = _source(db, source_id)
    if not source or not source.is_active:
        raise HTTPException(status_code=404, detail="active import source not found")
    settings = S3Settings.for_source(
        endpoint_url=source.endpoint_url,
        bucket=source.bucket,
        allowed_prefixes=tuple(source.allowed_prefixes),
        region=source.region,
        addressing_style=source.addressing_style,
        credential_env_prefix=source.credential_env_prefix,
    )
    return S3Browser.from_settings(settings)


def _source(db: Session, source_id: str) -> models.ImportSource | None:
    source = db.get(models.ImportSource, source_id)
    if source is None and source_id.lower() in {"mega", "s3", "mega-s4"}:
        source = db.scalar(select(models.ImportSource).where(models.ImportSource.provider == "s3", models.ImportSource.is_active.is_(True)).order_by(models.ImportSource.created_at))
    return source
