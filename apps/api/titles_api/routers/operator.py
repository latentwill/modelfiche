from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import current_workspace

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _count(db: Session, model, *criteria) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _preview_asset_id(db: Session, result_type: str, row) -> str | None:
    if result_type == "asset" and row.kind == models.AssetKind.image:
        return row.id
    if result_type == "dataset":
        return db.scalar(select(models.DatasetItem.asset_id).join(models.DatasetVersion, models.DatasetVersion.id == models.DatasetItem.dataset_version_id).where(models.DatasetVersion.dataset_id == row.id).order_by(models.DatasetVersion.version_number.desc(), models.DatasetItem.position).limit(1))
    if result_type == "run":
        return db.scalar(select(models.Sample.asset_id).where(models.Sample.run_id == row.id).order_by(models.Sample.step.desc(), models.Sample.created_at.desc()).limit(1))
    project_id = row.id if result_type == "project" else getattr(row, "project_id", None)
    if project_id:
        return db.scalar(select(models.EvalOutput.asset_id).join(models.EvalRun, models.EvalRun.id == models.EvalOutput.eval_run_id).join(models.EvalDefinition, models.EvalDefinition.id == models.EvalRun.definition_id).where(models.EvalDefinition.project_id == project_id).order_by(models.EvalOutput.created_at.desc()).limit(1))
    return None


@router.get("/dashboard")
def dashboard(db: DB, project_id: str | None = None):
    workspace_id = current_workspace(db).id
    workspace_projects = select(models.Project.id).where(models.Project.workspace_id == workspace_id)
    project_filter = (models.Project.id == project_id,) if project_id else (models.Project.workspace_id == workspace_id,)
    asset_filter = (models.Asset.project_id == project_id,) if project_id else (models.Asset.workspace_id == workspace_id,)
    run_filter = (models.TrainingRun.project_id == project_id,) if project_id else (models.TrainingRun.project_id.in_(workspace_projects),)
    dataset_filter = (models.Dataset.project_id == project_id,) if project_id else (models.Dataset.project_id.in_(workspace_projects),)
    model_filter = (models.Model.project_id == project_id,) if project_id else (models.Model.project_id.in_(workspace_projects),)

    recent_runs = db.scalars(
        select(models.TrainingRun).where(*run_filter).order_by(models.TrainingRun.updated_at.desc()).limit(8)
    ).all()
    recent_activity = db.scalars(
        select(models.ActivityEvent)
        .where(models.ActivityEvent.workspace_id == workspace_id, *(models.ActivityEvent.project_id == project_id,) if project_id else ())
        .order_by(models.ActivityEvent.created_at.desc())
        .limit(20)
    ).all()
    profile_ids = {event.profile_id for event in recent_activity if event.profile_id}
    profile_names = {
        profile.id: profile.display_name
        for profile in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()
    } if profile_ids else {}
    return {
        "scope": {"project_id": project_id},
        "counts": {
            "projects": _count(db, models.Project, *project_filter),
            "assets": _count(db, models.Asset, *asset_filter),
            "datasets": _count(db, models.Dataset, *dataset_filter),
            "runs": _count(db, models.TrainingRun, *run_filter),
            "models": _count(db, models.Model, *model_filter),
            "queued_jobs": _count(db, models.Job, models.Job.workspace_id == workspace_id, models.Job.state.in_([models.JobState.queued, models.JobState.running])),
        },
        "recent_runs": [
            {
                "id": run.id,
                "project_id": run.project_id,
                "name": run.name,
                "status": run.status,
                "trainer": run.trainer,
                "base_model": run.base_model,
                "updated_at": run.updated_at,
                "checkpoint_count": _count(db, models.Checkpoint, models.Checkpoint.run_id == run.id),
            }
            for run in recent_runs
        ],
        "recent_activity": [
            {
                "id": event.id,
                "event_type": event.action,
                "action": event.action,
                "summary": event.details.get("summary") or (
                    "Generation admitted"
                    if event.action == "fal.admission_admitted"
                    else event.action.replace(".", " ").replace("_", " ").title()
                ),
                "subject_type": event.subject_type,
                "subject_id": event.subject_id,
                "project_id": event.project_id,
                "profile_id": event.profile_id,
                "profile_name": profile_names.get(event.profile_id),
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in recent_activity
        ],
    }


@router.get("/search")
def global_search(db: DB, q: str = Query(min_length=1), project_id: str | None = None, limit: int = Query(30, ge=1, le=100)):
    needle = f"%{q.strip()}%"
    per_type = max(1, min(20, limit // 5 or 1))
    workspace_id = current_workspace(db).id
    workspace_projects = select(models.Project.id).where(models.Project.workspace_id == workspace_id)
    results: list[dict[str, Any]] = []

    project_query = select(models.Project).where(models.Project.workspace_id == workspace_id, or_(models.Project.title.ilike(needle), models.Project.description.ilike(needle)))
    if project_id:
        project_query = project_query.where(models.Project.id == project_id)
    results.extend({"type": "project", "id": row.id, "project_id": row.id, "title": row.title, "subtitle": row.description, "preview_asset_id": _preview_asset_id(db, "project", row)} for row in db.scalars(project_query.limit(per_type)))

    searches = (
        ("asset", models.Asset, models.Asset.name, models.Asset.project_id),
        ("dataset", models.Dataset, models.Dataset.name, models.Dataset.project_id),
        ("run", models.TrainingRun, models.TrainingRun.name, models.TrainingRun.project_id),
        ("model", models.Model, models.Model.name, models.Model.project_id),
        ("prompt_set", models.PromptSet, models.PromptSet.name, models.PromptSet.project_id),
    )
    for result_type, model, title_column, project_column in searches:
        query = select(model).where(title_column.ilike(needle), project_column.in_(workspace_projects))
        if project_id:
            query = query.where(project_column == project_id)
        for row in db.scalars(query.order_by(model.updated_at.desc()).limit(per_type)):
            results.append({"type": result_type, "id": row.id, "project_id": getattr(row, "project_id", None), "title": getattr(row, "name", ""), "updated_at": row.updated_at, "preview_asset_id": _preview_asset_id(db, result_type, row)})
    return {"query": q, "results": results[:limit], "count": min(len(results), limit)}
