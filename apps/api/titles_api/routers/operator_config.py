from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..integrations.fal.adapters import get_adapter
from ..integrations.llm import LLMConfigurationError, LLMResponseError, PromptGenerator
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


DEFAULT_OPERATOR_SETTINGS = {
    "llm": {"provider": "openai", "model": "", "base_url": None},
    "fal": {"default_endpoint": "ideogram/v4/lora", "max_parallel_jobs": 4},
}


class OperatorSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm: dict[str, Any] | None = None
    fal: dict[str, Any] | None = None


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=240)
    project_id: str | None = None
    asset_ids: list[str] = []
    dataset_version_ids: list[str] = []
    model_version_ids: list[str] = []
    eval_run_ids: list[str] = []
    include_files: bool = True
    include_reviews: bool = True


class PromptSetGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=12000)
    count: int = Field(default=6, ge=1, le=50)
    context: dict[str, Any] = Field(default_factory=dict)


class GeneratedPrompt(BaseModel):
    id: str
    prompt: str
    position: int
    metadata: dict[str, Any]


class PromptSetGenerateResponse(BaseModel):
    provider: Literal["openai", "openai-compatible", "anthropic", "gemini"]
    model: str
    prompts: list[GeneratedPrompt]
    persisted: bool = False


def _preference(db: Session) -> models.LocalPreference:
    preference = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "operator_settings"))
    if preference is None:
        preference = models.LocalPreference(key="operator_settings", value=DEFAULT_OPERATOR_SETTINGS)
        db.add(preference)
        db.flush()
    return preference


def _merge_settings(current: dict[str, Any], patch: OperatorSettingsUpdate) -> dict[str, Any]:
    result = {section: dict(current.get(section, defaults)) for section, defaults in DEFAULT_OPERATOR_SETTINGS.items()}
    for section, values in patch.model_dump(exclude_none=True).items():
        allowed = set(DEFAULT_OPERATOR_SETTINGS[section])
        unknown = set(values) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail=f"unsupported {section} settings: {', '.join(sorted(unknown))}")
        result[section].update(values)
    concurrency = result["fal"].get("max_parallel_jobs")
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
        raise HTTPException(status_code=422, detail="fal.max_parallel_jobs must be between 1 and 32")
    llm_provider = str(result["llm"].get("provider") or "").lower()
    if llm_provider not in {"openai", "openai-compatible", "openai_compatible", "anthropic", "claude", "gemini", "google"}:
        raise HTTPException(status_code=422, detail="unsupported llm.provider")
    if llm_provider in {"openai-compatible", "openai_compatible"} and not result["llm"].get("base_url"):
        raise HTTPException(status_code=422, detail="llm.base_url is required for openai-compatible")
    endpoint = result["fal"].get("default_endpoint")
    try:
        get_adapter(str(endpoint))
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/operator-settings")
@router.get("/settings/operator")
def operator_settings(db: DB):
    values = _preference(db).value
    settings = get_settings()
    return {
        **values,
        "credentials": {
            "llm_configured": bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
            "fal_configured": bool(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")),
            "storage_credentials_stored": False,
        },
        "paths": {"asset_root": str(settings.asset_root), "cache_root": str(settings.cache_root), "export_root": str(settings.export_root)},
    }


@router.patch("/operator-settings")
@router.patch("/settings/operator")
def update_operator_settings(body: OperatorSettingsUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    preference = _preference(db)
    values = _merge_settings(preference.value, body)
    preference.value = values
    record_activity(db, action="settings.updated", subject_type="workspace", subject_id=current_workspace(db).id, profile_id=active_profile(db, x_profile_id).id)
    db.commit()
    return operator_settings(db)


@router.post("/operator-settings/test/{provider}")
@router.post("/settings/operator/test/{provider}")
def test_operator_configuration(provider: Literal["llm", "fal", "storage"], db: DB):
    values = _preference(db).value
    if provider == "llm":
        llm = values.get("llm", {})
        configured = bool(llm.get("provider") and llm.get("model"))
        key_present = bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
        return {"provider": provider, "ok": configured and key_present, "mode": "configuration_only", "configured": configured, "credential_present": key_present, "network_called": False}
    if provider == "fal":
        endpoint = values.get("fal", {}).get("default_endpoint")
        try:
            get_adapter(str(endpoint))
            adapter_ok = True
        except KeyError:
            adapter_ok = False
        key_present = bool(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))
        return {"provider": provider, "ok": adapter_ok and key_present, "mode": "configuration_only", "endpoint": endpoint, "credential_present": key_present, "network_called": False}
    settings = get_settings()
    local_ok = all(path.exists() and path.is_dir() for path in (settings.asset_root, settings.cache_root, settings.export_root))
    return {"provider": provider, "ok": local_ok, "mode": "configuration_only", "local_paths_ok": local_ok, "network_called": False}


@router.post("/prompt-sets/generate", response_model=PromptSetGenerateResponse)
def generate_prompt_set(body: PromptSetGenerateRequest, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    """Explicit, non-persisting generation action for the editable prompt-set builder."""
    llm = _preference(db).value.get("llm", {})
    try:
        result = PromptGenerator().generate(
            provider=str(llm.get("provider") or ""),
            model=str(llm.get("model") or ""),
            base_url=llm.get("base_url"),
            instruction=body.instruction,
            count=body.count,
            context=body.context,
        )
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    requested_project_id = body.context.get("project_id")
    project_id = str(requested_project_id) if requested_project_id and db.get(models.Project, str(requested_project_id)) else None
    record_activity(
        db, action="prompt_set.generated_preview", subject_type="workspace", subject_id=current_workspace(db).id,
        profile_id=active_profile(db, x_profile_id).id, project_id=project_id,
        details={"provider": result["provider"], "model": result["model"], "prompt_count": len(result["prompts"])},
    )
    db.commit()
    return {**result, "persisted": False}


def _transfer_row(job: models.Job) -> dict[str, Any]:
    return {
        "id": job.id, "name": job.payload.get("name") or f"Export {job.created_at:%Y-%m-%d %H:%M}",
        "direction": "export", "state": job.state.value if hasattr(job.state, "value") else job.state,
        "progress": job.progress, "selection": {key: job.payload.get(key) for key in ("project_id", "asset_ids", "dataset_version_ids", "model_version_ids", "eval_run_ids")},
        "result": job.result, "error": job.error, "created_at": job.created_at, "updated_at": job.updated_at,
        "download_ready": bool(job.state == models.JobState.succeeded and job.result.get("path")),
    }


@router.post("/transfers", status_code=status.HTTP_202_ACCEPTED)
def create_transfer(body: TransferCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    if not (body.project_id or body.asset_ids or body.dataset_version_ids or body.model_version_ids or body.eval_run_ids):
        raise HTTPException(status_code=422, detail="an export selection is required")
    if body.project_id:
        get_or_404(db, models.Project, body.project_id)
    actor = active_profile(db, x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, kind="export.package", profile_id=actor.id, payload=body.model_dump())
    db.add(job)
    db.flush()
    record_activity(db, action="export.queued", subject_type="job", subject_id=job.id, profile_id=actor.id, project_id=body.project_id)
    db.commit()
    return _transfer_row(job)


@router.get("/transfers")
def transfers(db: DB, state: str | None = None):
    query = select(models.Job).where(models.Job.kind == "export.package").order_by(models.Job.created_at.desc())
    if state:
        query = query.where(models.Job.state == state)
    return [_transfer_row(job) for job in db.scalars(query)]


@router.get("/transfers/{transfer_id}")
def transfer(transfer_id: str, db: DB):
    job = get_or_404(db, models.Job, transfer_id)
    if job.kind != "export.package":
        raise HTTPException(status_code=404, detail="Transfer not found")
    return _transfer_row(job)


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    job = get_or_404(db, models.Job, transfer_id)
    if job.kind != "export.package":
        raise HTTPException(status_code=404, detail="Transfer not found")
    if job.state in (models.JobState.succeeded, models.JobState.failed, models.JobState.canceled):
        raise HTTPException(status_code=409, detail="transfer is already terminal")
    job.cancel_requested = True
    record_activity(db, action="export.cancel_requested", subject_type="job", subject_id=job.id, profile_id=active_profile(db, x_profile_id).id, project_id=job.payload.get("project_id"))
    db.commit()
    return _transfer_row(job)


@router.get("/transfers/{transfer_id}/download")
def download_transfer(transfer_id: str, db: DB):
    job = get_or_404(db, models.Job, transfer_id)
    if job.kind != "export.package" or job.state != models.JobState.succeeded:
        raise HTTPException(status_code=409, detail="transfer is not ready for download")
    raw_path = job.result.get("path")
    if not raw_path:
        raise HTTPException(status_code=404, detail="export package is missing")
    root = get_settings().export_root.resolve()
    path = Path(str(raw_path)).resolve()
    if not (path == root or root in path.parents):
        raise HTTPException(status_code=403, detail="export package is outside the configured export root")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="export package is missing")
    return FileResponse(path, filename=job.result.get("filename") or path.name, media_type="application/zip")
