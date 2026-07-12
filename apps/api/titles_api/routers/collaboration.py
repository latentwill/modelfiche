from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def dump(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


def attributed(db: Session, row) -> dict[str, Any]:
    profile = db.get(models.UserProfile, row.profile_id) if row.profile_id else None
    return {
        **dump(row),
        "subject": f"{row.subject_type}:{row.subject_id}",
        "profile_name": profile.display_name if profile else None,
    }


def _lineage_preview(db: Session, edge: models.LineageEdge) -> str | None:
    for kind, identifier in ((edge.target_type, edge.target_id), (edge.source_type, edge.source_id)):
        if kind == "eval_output":
            output = db.get(models.EvalOutput, identifier)
            if output:
                return output.asset_id
        if kind == "checkpoint":
            asset_id = db.scalar(select(models.Sample.asset_id).where(models.Sample.checkpoint_id == identifier).order_by(models.Sample.created_at.desc()).limit(1))
            if asset_id:
                return asset_id
        if kind == "training_run":
            asset_id = db.scalar(select(models.Sample.asset_id).where(models.Sample.run_id == identifier).order_by(models.Sample.created_at.desc()).limit(1))
            if asset_id:
                return asset_id
        if kind == "asset":
            return identifier
    return None


@router.post("/reviews", status_code=status.HTTP_201_CREATED)
def create_review(body: schemas.ReviewCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    if body.rating is None and body.decision is None:
        raise HTTPException(status_code=422, detail="rating or decision is required")
    actor = active_profile(db, body.profile_id or x_profile_id)
    review = models.Review(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(review)
    db.flush()
    record_activity(db, action="review.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"review_id": review.id, "rating": body.rating, "decision": body.decision})
    db.commit()
    return attributed(db, review)


@router.get("/reviews")
def reviews(db: DB, subject_type: str | None = None, subject_id: str | None = None, subject: str | None = None, project_id: str | None = None, decision: str | None = None, profile_id: str | None = None):
    query = select(models.Review).order_by(models.Review.created_at.desc())
    if subject and ":" in subject:
        subject_type, subject_id = subject.split(":", 1)
    if subject_type:
        query = query.where(models.Review.subject_type == subject_type)
    if subject_id:
        query = query.where(models.Review.subject_id == subject_id)
    if decision:
        query = query.where(models.Review.decision == decision)
    if profile_id:
        query = query.where(models.Review.profile_id == profile_id)
    # Polymorphic subjects cannot be joined generically; project filtering covers subjects with direct activity attribution.
    if project_id:
        subject_ids = select(models.ActivityEvent.subject_id).where(models.ActivityEvent.project_id == project_id)
        query = query.where(models.Review.subject_id.in_(subject_ids))
    return [attributed(db, row) for row in db.scalars(query).all()]


@router.post("/comments", status_code=status.HTTP_201_CREATED)
def create_comment(body: schemas.CommentCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    comment = models.Comment(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(comment)
    db.flush()
    record_activity(db, action="comment.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"comment_id": comment.id})
    db.commit()
    return attributed(db, comment)


@router.get("/comments")
def comments(db: DB, subject_type: str | None = None, subject_id: str | None = None, subject: str | None = None):
    if subject and ":" in subject:
        subject_type, subject_id = subject.split(":", 1)
    if not subject_type or not subject_id:
        raise HTTPException(status_code=422, detail="subject_type and subject_id, or subject, are required")
    query = select(models.Comment).where(models.Comment.subject_type == subject_type, models.Comment.subject_id == subject_id).order_by(models.Comment.created_at)
    return [attributed(db, row) for row in db.scalars(query).all()]


@router.post("/notes", status_code=status.HTTP_201_CREATED)
def create_note(body: schemas.NoteCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    if body.supersedes_id:
        previous = get_or_404(db, models.Note, body.supersedes_id)
        if previous.subject_type != body.subject_type or previous.subject_id != body.subject_id:
            raise HTTPException(status_code=400, detail="superseded note has another subject")
        revision = previous.revision + 1
    else:
        revision = 1
    note = models.Note(workspace_id=current_workspace(db).id, profile_id=actor.id, revision=revision, **body.model_dump(exclude={"profile_id"}))
    db.add(note)
    db.flush()
    record_activity(db, action="note.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"note_id": note.id, "revision": revision})
    db.commit()
    return attributed(db, note)


@router.get("/notes")
def notes(db: DB, subject_type: str, subject_id: str):
    return [attributed(db, row) for row in db.scalars(select(models.Note).where(models.Note.subject_type == subject_type, models.Note.subject_id == subject_id).order_by(models.Note.created_at.desc())).all()]


@router.post("/lineage", status_code=status.HTTP_201_CREATED)
def create_lineage(body: schemas.LineageCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    edge = models.LineageEdge(workspace_id=current_workspace(db).id, metadata_=body.metadata, **body.model_dump(exclude={"metadata"}))
    db.add(edge)
    db.flush()
    record_activity(db, action="lineage.created", subject_type=body.target_type, subject_id=body.target_id, profile_id=active_profile(db, x_profile_id).id, details={"edge_id": edge.id, "relationship": edge.relationship})
    db.commit()
    return dump(edge)


@router.get("/lineage")
def lineage(db: DB, subject_type: str, subject_id: str):
    query = select(models.LineageEdge).where(or_(
        (models.LineageEdge.source_type == subject_type) & (models.LineageEdge.source_id == subject_id),
        (models.LineageEdge.target_type == subject_type) & (models.LineageEdge.target_id == subject_id),
    )).order_by(models.LineageEdge.created_at)
    return [{**dump(row), "preview_asset_id": _lineage_preview(db, row)} for row in db.scalars(query).all()]


@router.get("/activity")
def activity(db: DB, project_id: str | None = None, subject_type: str | None = None, subject_id: str | None = None, profile_id: str | None = None, since: str | None = None, limit: int = Query(100, le=500)):
    query = select(models.ActivityEvent).order_by(models.ActivityEvent.created_at.desc())
    if project_id:
        query = query.where(models.ActivityEvent.project_id == project_id)
    if subject_type:
        query = query.where(models.ActivityEvent.subject_type == subject_type)
    if subject_id:
        query = query.where(models.ActivityEvent.subject_id == subject_id)
    if profile_id:
        query = query.where(models.ActivityEvent.profile_id == profile_id)
    if since:
        from datetime import datetime
        try:
            since_value = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="since must be an ISO-8601 datetime") from exc
        query = query.where(models.ActivityEvent.created_at >= since_value)
    rows = db.scalars(query.limit(limit)).all()
    profile_ids = {row.profile_id for row in rows if row.profile_id}
    names = {
        profile.id: profile.display_name
        for profile in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()
    } if profile_ids else {}
    return [{
        **dump(row),
        "event_type": row.action,
        "summary": row.details.get("summary") or row.action.replace(".", " ").replace("_", " ").title(),
        "profile_name": names.get(row.profile_id),
    } for row in rows]


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: schemas.JobCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if body.idempotency_key:
            existing = db.scalar(select(models.Job).where(models.Job.idempotency_key == body.idempotency_key))
            if existing:
                return dump(existing)
        raise
    record_activity(db, action="job.queued", subject_type="job", subject_id=job.id, profile_id=actor.id, details={"kind": job.kind})
    db.commit()
    return dump(job)


@router.get("/jobs")
def jobs(db: DB, state: str | None = None, kind: str | None = None):
    query = select(models.Job).order_by(models.Job.created_at.desc())
    if state:
        query = query.where(models.Job.state == state)
    if kind:
        query = query.where(models.Job.kind == kind)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/jobs/{job_id}")
def job(job_id: str, db: DB):
    return dump(get_or_404(db, models.Job, job_id))


@router.get("/jobs/{job_id}/logs")
def job_logs(job_id: str, db: DB, tail: int = Query(100, ge=1, le=1000)):
    job = get_or_404(db, models.Job, job_id)
    events = db.scalars(select(models.ActivityEvent).where(models.ActivityEvent.subject_type == "job", models.ActivityEvent.subject_id == job.id).order_by(models.ActivityEvent.created_at.desc()).limit(tail)).all()
    rows = [{"timestamp": event.created_at, "level": "info", "event": event.action, "details": event.details} for event in reversed(events)]
    if job.error:
        rows.append({"timestamp": job.updated_at, "level": "error", "event": "job.error", "message": job.error})
    if job.result:
        rows.append({"timestamp": job.updated_at, "level": "info", "event": "job.result", "details": job.result})
    return rows[-tail:]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    job = get_or_404(db, models.Job, job_id)
    if job.state in (models.JobState.succeeded, models.JobState.failed, models.JobState.canceled):
        raise HTTPException(status_code=409, detail="job is already terminal")
    job.cancel_requested = True
    record_activity(db, action="job.cancel_requested", subject_type="job", subject_id=job.id, profile_id=active_profile(db, x_profile_id).id)
    db.commit()
    return dump(job)


@router.post("/prompt-sets", status_code=status.HTTP_201_CREATED)
def create_prompt_set(body: schemas.PromptSetCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    get_or_404(db, models.Project, body.project_id)
    prompt_set = models.PromptSet(project_id=body.project_id, name=body.name)
    db.add(prompt_set)
    db.flush()
    for index, value in enumerate(body.prompts):
        if isinstance(value, dict):
            text = value.get("prompt") or value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=422, detail=f"prompt at index {index} requires prompt or text")
            metadata = {key: item for key, item in value.items() if key not in {"prompt", "text"}}
        else:
            text, metadata = value, {}
        db.add(models.Prompt(prompt_set_id=prompt_set.id, text=text, position=index, metadata_=metadata))
    record_activity(db, action="prompt_set.created", subject_type="prompt_set", subject_id=prompt_set.id, profile_id=active_profile(db, x_profile_id).id, project_id=body.project_id, details={"prompts": len(body.prompts)})
    db.commit()
    return dump(prompt_set)


@router.get("/prompt-sets")
def prompt_sets(db: DB, project_id: str | None = None):
    query = select(models.PromptSet).order_by(models.PromptSet.updated_at.desc())
    if project_id:
        query = query.where(models.PromptSet.project_id == project_id)
    rows = []
    for row in db.scalars(query).all():
        project = db.get(models.Project, row.project_id)
        rows.append({
            **dump(row),
            "project_name": project.title if project else None,
            "prompt_count": int(db.scalar(select(func.count()).select_from(models.Prompt).where(models.Prompt.prompt_set_id == row.id)) or 0),
        })
    return rows


@router.get("/prompt-sets/{prompt_set_id}")
def prompt_set_detail(prompt_set_id: str, db: DB):
    prompt_set = get_or_404(db, models.PromptSet, prompt_set_id)
    prompts = db.scalars(select(models.Prompt).where(models.Prompt.prompt_set_id == prompt_set.id).order_by(models.Prompt.position)).all()
    return {**dump(prompt_set), "prompts": [dump(prompt) for prompt in prompts]}


@router.put("/prompt-sets/{prompt_set_id}")
def update_prompt_set(prompt_set_id: str, body: schemas.PromptSetUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    previous = get_or_404(db, models.PromptSet, prompt_set_id)
    new = models.PromptSet(project_id=previous.project_id, name=body.name or previous.name, version=previous.version + 1)
    db.add(new)
    db.flush()
    texts = body.prompts
    if texts is None:
        texts = list(db.scalars(select(models.Prompt.text).where(models.Prompt.prompt_set_id == previous.id).order_by(models.Prompt.position)))
    for position, value in enumerate(texts):
        if isinstance(value, dict):
            text = value.get("prompt") or value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=422, detail=f"prompt at index {position} requires prompt or text")
            metadata = {key: item for key, item in value.items() if key not in {"prompt", "text"}}
        else:
            text, metadata = value, {}
        db.add(models.Prompt(prompt_set_id=new.id, text=text, position=position, metadata_=metadata))
    record_activity(db, action="prompt_set.versioned", subject_type="prompt_set", subject_id=new.id, profile_id=active_profile(db, x_profile_id).id, project_id=new.project_id, details={"previous_id": previous.id, "version": new.version})
    db.commit()
    return prompt_set_detail(new.id, db)


@router.post("/prompt-sets/{prompt_set_id}/versions", status_code=status.HTTP_201_CREATED)
def version_prompt_set(prompt_set_id: str, body: schemas.PromptSetUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return update_prompt_set(prompt_set_id, body, db, x_profile_id)


@router.post("/eval-definitions", status_code=status.HTTP_201_CREATED)
def create_eval_definition(body: schemas.EvalDefinitionCreate, db: DB):
    try:
        from ..integrations.fal.adapters import get_adapter
        get_adapter(body.endpoint)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_or_404(db, models.Project, body.project_id)
    if body.prompt_set_id:
        prompt_set = get_or_404(db, models.PromptSet, body.prompt_set_id)
        if prompt_set.project_id != body.project_id:
            raise HTTPException(status_code=409, detail="prompt set belongs to another project")
    if body.model_version_id:
        version = get_or_404(db, models.ModelVersion, body.model_version_id)
        model = get_or_404(db, models.Model, version.model_id)
        if model.project_id != body.project_id:
            raise HTTPException(status_code=409, detail="model version belongs to another project")
    definition = models.EvalDefinition(**body.model_dump())
    db.add(definition)
    db.commit()
    return dump(definition)


@router.get("/eval-definitions")
def eval_definitions(db: DB, project_id: str | None = None):
    query = select(models.EvalDefinition).order_by(models.EvalDefinition.updated_at.desc())
    if project_id:
        query = query.where(models.EvalDefinition.project_id == project_id)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/eval-definitions/{definition_id}")
def eval_definition(definition_id: str, db: DB):
    return dump(get_or_404(db, models.EvalDefinition, definition_id))


@router.post("/eval-runs", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def create_eval_run(body: schemas.EvalRunCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    if not body.dry_run:
        raise HTTPException(status_code=409, detail={"code": "FAL_ADMISSION_REQUIRED", "message": "Create and admit a FAL admission before provider work."})
    if body.definition_id:
        definition = get_or_404(db, models.EvalDefinition, body.definition_id)
    else:
        endpoint = body.endpoint or body.endpoint_adapter
        try:
            from ..integrations.fal.adapters import get_adapter
            get_adapter(str(endpoint))
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        version = get_or_404(db, models.ModelVersion, str(body.model_version_id))
        model = get_or_404(db, models.Model, version.model_id)
        prompt_set = get_or_404(db, models.PromptSet, str(body.prompt_set_id))
        if prompt_set.project_id != model.project_id:
            raise HTTPException(status_code=400, detail="model version and prompt set belong to different projects")
        definition = db.scalar(select(models.EvalDefinition).where(
            models.EvalDefinition.project_id == model.project_id,
            models.EvalDefinition.endpoint == endpoint,
            models.EvalDefinition.model_version_id == version.id,
            models.EvalDefinition.prompt_set_id == prompt_set.id,
            models.EvalDefinition.parameters == body.parameters,
        ))
        if definition is None:
            definition = models.EvalDefinition(project_id=model.project_id, name=f"{model.name} / {prompt_set.name}", endpoint=str(endpoint), model_version_id=version.id, prompt_set_id=prompt_set.id, parameters=body.parameters)
            db.add(definition)
            db.flush()
    checkpoint_id = body.checkpoint_id
    if checkpoint_id:
        get_or_404(db, models.Checkpoint, checkpoint_id)
    elif definition.model_version_id:
        checkpoint_id = get_or_404(db, models.ModelVersion, definition.model_version_id).checkpoint_id
    actor = active_profile(db, x_profile_id)
    run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint_id, status="preview" if body.dry_run else "queued", parameters_snapshot={**definition.parameters, **body.parameters})
    db.add(run)
    db.flush()
    if body.dry_run:
        db.commit()
        return {**dump(run), "definition_id": definition.id, "dry_run": True, "job_id": None}
    job = models.Job(workspace_id=current_workspace(db).id, kind="fal.eval_batch", profile_id=actor.id, payload={"eval_run_id": run.id})
    db.add(job)
    record_activity(db, action="eval.queued", subject_type="eval_run", subject_id=run.id, profile_id=actor.id, project_id=definition.project_id, details={"job_id": job.id})
    db.commit()
    return {**dump(run), "job_id": job.id}


@router.get("/eval-runs/{run_id}")
def eval_run(run_id: str, db: DB):
    run = get_or_404(db, models.EvalRun, run_id)
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    outputs = db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == run.id)).all()
    jobs = db.scalars(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == run.id).order_by(models.Job.created_at.desc())).all()
    latest_job = jobs[0] if jobs else None
    output_rows = []
    for output in outputs:
        prompt = db.get(models.Prompt, output.prompt_id) if output.prompt_id else None
        asset = db.get(models.Asset, output.asset_id)
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == output.asset_id))
        output_rows.append(
            {
                **dump(output),
                "asset_revision_id": output.asset_id,
                "prompt": prompt.text if prompt else None,
                "filename": asset.name if asset else None,
                "mime_type": asset.mime_type if asset else None,
                "asset_metadata": dict(asset.metadata_ or {}) if asset else {},
                "width": image.width if image else None,
                "height": image.height if image else None,
            }
        )
    return {**dump(run), "name": definition.name, "endpoint_adapter": definition.endpoint, "model_version_id": definition.model_version_id, "prompt_set_id": definition.prompt_set_id, "parameters": run.parameters_snapshot, "progress": latest_job.progress if latest_job else (1 if run.status == "succeeded" else 0), "job_id": latest_job.id if latest_job else None, "outputs": output_rows}


@router.get("/eval-runs")
def eval_runs(db: DB, definition_id: str | None = None, project_id: str | None = None, run_status: str | None = Query(None, alias="status")):
    query = select(models.EvalRun).order_by(models.EvalRun.created_at.desc())
    if definition_id:
        query = query.where(models.EvalRun.definition_id == definition_id)
    if run_status:
        query = query.where(models.EvalRun.status == run_status)
    if project_id:
        query = query.join(models.EvalDefinition, models.EvalDefinition.id == models.EvalRun.definition_id).where(models.EvalDefinition.project_id == project_id)
    rows = []
    for row in db.scalars(query):
        definition = db.get(models.EvalDefinition, row.definition_id)
        version = db.get(models.ModelVersion, definition.model_version_id) if definition and definition.model_version_id else None
        prompt_set = db.get(models.PromptSet, definition.prompt_set_id) if definition else None
        job = db.scalar(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == row.id).order_by(models.Job.created_at.desc()))
        rows.append({**dump(row), "name": definition.name if definition else None, "endpoint_adapter": definition.endpoint if definition else None, "model_version_id": definition.model_version_id if definition else None, "model_version_name": version.name if version else None, "prompt_set_id": definition.prompt_set_id if definition else None, "prompt_set_name": prompt_set.name if prompt_set else None, "parameters": row.parameters_snapshot, "progress": job.progress if job else (1 if row.status == "succeeded" else 0), "job_id": job.id if job else None})
    return rows


@router.get("/eval-runs/{run_id}/outputs")
def eval_outputs(run_id: str, db: DB):
    get_or_404(db, models.EvalRun, run_id)
    rows = []
    for output in db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == run_id).order_by(models.EvalOutput.created_at)).all():
        prompt = db.get(models.Prompt, output.prompt_id) if output.prompt_id else None
        asset = db.get(models.Asset, output.asset_id)
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == output.asset_id))
        rows.append(
            {
                **dump(output),
                "asset_revision_id": output.asset_id,
                "prompt": prompt.text if prompt else None,
                "filename": asset.name if asset else None,
                "mime_type": asset.mime_type if asset else None,
                "asset_metadata": dict(asset.metadata_ or {}) if asset else {},
                "width": image.width if image else None,
                "height": image.height if image else None,
            }
        )
    return rows


@router.post("/eval-runs/{run_id}/cancel")
def cancel_eval_run(run_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    run = get_or_404(db, models.EvalRun, run_id)
    if run.status in {"succeeded", "failed", "canceled"}:
        raise HTTPException(status_code=409, detail="eval run is already terminal")
    run.status = "canceled"
    jobs = db.scalars(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == run.id)).all()
    for job in jobs:
        job.cancel_requested = True
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    record_activity(db, action="eval.canceled", subject_type="eval_run", subject_id=run.id, profile_id=active_profile(db, x_profile_id).id, project_id=definition.project_id)
    db.commit()
    return dump(run)


@router.post("/grid-definitions", status_code=status.HTTP_201_CREATED)
def create_grid_definition(body: schemas.GridDefinitionCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    definition = get_or_404(db, models.EvalDefinition, body.eval_definition_id)
    if definition.project_id != body.project_id:
        raise HTTPException(status_code=400, detail="eval definition belongs to another project")
    grid = models.GridDefinition(**body.model_dump())
    db.add(grid)
    db.flush()
    record_activity(db, action="grid.created", subject_type="grid_definition", subject_id=grid.id, profile_id=active_profile(db, x_profile_id).id, project_id=grid.project_id)
    db.commit()
    return dump(grid)


@router.get("/grid-definitions")
def grid_definitions(db: DB, project_id: str | None = None):
    query = select(models.GridDefinition).order_by(models.GridDefinition.created_at.desc())
    if project_id:
        query = query.where(models.GridDefinition.project_id == project_id)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/grid-definitions/{grid_id}")
def grid_definition(grid_id: str, db: DB):
    grid = get_or_404(db, models.GridDefinition, grid_id)
    cells = db.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid.id).order_by(models.GridCell.y_index, models.GridCell.x_index)).all()
    jobs = db.scalars(select(models.Job).where(models.Job.payload["grid_definition_id"].as_string() == grid.id).order_by(models.Job.created_at.desc())).all()
    x_values, y_values = grid.x_axis.get("values", []), grid.y_axis.get("values", [])
    cell_rows = []
    for cell in cells:
        output = db.get(models.EvalOutput, cell.eval_output_id)
        cell_rows.append({
            **dump(cell),
            "asset_id": output.asset_id if output else None,
            "asset_revision_id": output.asset_id if output else None,
            "seed": output.seed if output else None,
            "x_value": x_values[cell.x_index] if cell.x_index < len(x_values) else None,
            "y_value": y_values[cell.y_index] if cell.y_index < len(y_values) else None,
        })
    definition = db.get(models.EvalDefinition, grid.eval_definition_id)
    return {**dump(grid), "endpoint_adapter": definition.endpoint if definition else None, "model_version_id": definition.model_version_id if definition else None, "prompt_set_id": definition.prompt_set_id if definition else None, "parameters": definition.parameters if definition else {}, "checkpoint_revision_id": (db.get(models.ModelVersion, definition.model_version_id).checkpoint_revision_id if definition and definition.model_version_id and db.get(models.ModelVersion, definition.model_version_id) else None), "cells": cell_rows, "jobs": [dump(job) for job in jobs]}


@router.post("/grid-definitions/{grid_id}/runs", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def enqueue_grid(grid_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    raise HTTPException(status_code=409, detail={"code": "FAL_ADMISSION_REQUIRED", "message": "Create and admit a FAL admission before provider work."})
    grid = get_or_404(db, models.GridDefinition, grid_id)
    actor = active_profile(db, x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, kind="fal.grid_batch", profile_id=actor.id, payload={"grid_definition_id": grid.id})
    db.add(job)
    db.flush()
    record_activity(db, action="grid.queued", subject_type="grid_definition", subject_id=grid.id, profile_id=actor.id, project_id=grid.project_id, details={"job_id": job.id})
    db.commit()
    return {"grid_definition_id": grid.id, "job_id": job.id, "state": job.state.value}
