from __future__ import annotations

import mimetypes
import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.storage.network import SafeHttpTransport
from titles_api.storage.repository import StorageRepository
from titles_api.image_provenance import fal_generated_at
from titles_api.settings import get_settings



class SQLAlchemyEvalOutputSink:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cache: AssetCache,
        *,
        artifact_transport: SafeHttpTransport | None = None,
    ):
        self.session_factory = session_factory
        self.cache = cache
        self.artifact_transport = artifact_transport or SafeHttpTransport()

    def provider_submitted(self, eval_run_id: str, request_id: str, grid_cell_id: str | None = None) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            if request_id not in run.provider_job_ids:
                run.provider_job_ids = [*run.provider_job_ids, request_id]
            run.status = "running"
            if run.plan_id and grid_cell_id:
                grid_ids = list(session.scalars(select(models.GridDefinition.id).where(models.GridDefinition.plan_id == run.plan_id)))
                for grid_id in grid_ids:
                    cell = session.scalar(select(models.GridCell).where(models.GridCell.id == grid_cell_id, models.GridCell.grid_definition_id == grid_id).limit(1))
                    if cell and cell.status in {"pending", "admitted"}:
                        cell.status = "running"

    def ingest_outputs(self, eval_run_id: str, prompt_id: str | None, response: dict[str, Any]) -> dict[str, Any]:
        images = response.get("images") or []
        if not isinstance(images, list):
            raise ValueError("FAL result images must be a list")
        with self.session_factory() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            definition = _required(session, models.EvalDefinition, run.definition_id)
            project = _required(session, models.Project, definition.project_id)
            prompt = session.get(models.Prompt, prompt_id) if prompt_id else None
            prompt_text = prompt.text if prompt else str(response.get("prompt") or "") or None
            logical_prompt_id = prompt_id or (str(response.get("_inline_prompt_id")) if response.get("_inline_prompt_id") else None)
            grid_metadata = response.pop("_grid_metadata", None)
            response_metadata = {key: value for key, value in response.items() if key != "images"}
            request_parameters = dict(run.parameters_snapshot or definition.parameters or {})
            if isinstance(grid_metadata, dict) and isinstance(grid_metadata.get("parameters"), dict):
                request_parameters = dict(grid_metadata["parameters"])
            request_ids = [str(response["_request_id"])] if response.get("_request_id") else list(run.provider_job_ids or [])
            generated_at = fal_generated_at(response)
            output_count = 0
            output_ids: list[str] = []
            for index, image in enumerate(images):
                if not isinstance(image, dict) or not image.get("url"):
                    continue
                source_url = str(image["url"])
                entry = self.cache.persist(self._download(source_url, image.get("file_size")), get_settings().asset_root)
                if source_url.startswith("data:"):
                    extension = mimetypes.guess_extension(source_url[5:].split(";", 1)[0]) or ".bin"
                    filename = f"fal-output-{index}{extension}"
                else:
                    filename = Path(urlsplit(source_url).path).name or f"fal-output-{index}.png"
                asset = models.Asset(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    kind=models.AssetKind.image,
                    name=filename,
                    mime_type=image.get("content_type") or mimetypes.guess_type(filename)[0],
                    sha256=entry.sha256,
                    metadata_={
                        "category": "eval_output",
                        "provider": "fal",
                        "caption": prompt_text,
                        "prompt": prompt_text,
                        "prompt_id": logical_prompt_id,
                        "endpoint": response.get("_endpoint_id") or definition.endpoint,
                        "seed": response.get("seed"),
                        "provider_request_ids": request_ids,
                        "parameters": request_parameters,
                        "fal_request_input": request_parameters,
                        **{
                            key: grid_metadata[key]
                            for key in (
                                "checkpoint_id", "checkpoint_name", "checkpoint_step",
                                "run_id", "run_name", "base_model", "model_id", "model_name",
                                "model_version_id", "model_version_name", "checkpoint_revision_id",
                                "grid_cell_id", "grid_definition_id", "x_index", "y_index",
                                "z_index", "coordinates",
                            )
                            if isinstance(grid_metadata, dict) and grid_metadata.get(key) is not None
                        },
                        **({"generated_at": generated_at.isoformat()} if generated_at else {}),
                    },
                )
                session.add(asset)
                session.flush()
                session.add(models.ActivityEvent(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    profile_id=None,
                    action="asset.generated",
                    subject_type="asset",
                    subject_id=asset.id,
                    details={
                        "provider": "fal",
                        "endpoint": definition.endpoint,
                        "eval_run_id": run.id,
                    },
                ))
                location = StorageRepository(session, project.workspace_id).attach_verified_local_location(
                    asset_id=asset.id,
                    uri=str(entry.path),
                    size=entry.size,
                    sha256=entry.sha256,
                    mime_type=asset.mime_type or "application/octet-stream",
                )
                location.relative_path = str(entry.path.relative_to(get_settings().asset_root.resolve()))
                asset.preferred_location_id = location.id
                width, height = image.get("width"), image.get("height")
                if width or height:
                    session.add(models.ImageMetadata(asset_id=asset.id, width=width, height=height))
                output = models.EvalOutput(
                    eval_run_id=run.id,
                    prompt_id=prompt_id,
                    asset_id=asset.id,
                    seed=response.get("seed"),
                    provider_metadata={
                        **{key: value for key, value in image.items() if key != "url"},
                        "source_url": None if source_url.startswith("data:") else source_url,
                        "source_kind": "inline_data" if source_url.startswith("data:") else "url",
                        "prompt": prompt_text,
                        "inline_prompt_id": None if prompt_id else logical_prompt_id,
                        "endpoint": response.get("_endpoint_id") or definition.endpoint,
                        "request_ids": request_ids,
                        "_request_input": request_parameters,
                        **({"grid_metadata": grid_metadata} if isinstance(grid_metadata, dict) else {}),
                        "response": response_metadata,
                    },
                    generated_at=generated_at,
                )
                session.add(output)
                session.flush()
                if run.checkpoint_id:
                    session.add(
                        models.LineageEdge(
                            workspace_id=project.workspace_id,
                            source_type="checkpoint",
                            source_id=run.checkpoint_id,
                            target_type="eval_output",
                            target_id=output.id,
                            relationship="generated_from",
                            metadata_={"endpoint": definition.endpoint, "prompt_id": prompt_id},
                        )
                    )
                output_count += 1
                output_ids.append(output.id)
            session.commit()
            return {"outputs": output_count, "output_ids": output_ids}

    def submission_intent(
        self,
        eval_run_id: str,
        *,
        prompt_id: str | None = None,
        grid_cell_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the pre-materialized intent for one admitted subject."""
        with self.session_factory() as session:
            admission_id = session.scalar(
                select(models.FalAdmission.id).where(models.FalAdmission.eval_run_id == eval_run_id).limit(1)
            )
            if admission_id is None:
                return None
            subjects = list(
                session.scalars(
                    select(models.FalSubject)
                    .where(models.FalSubject.admission_id == admission_id)
                    .order_by(models.FalSubject.ordinal, models.FalSubject.id)
                )
            )
            for subject in subjects:
                definition = dict(subject.definition or {})
                if prompt_id is not None and str(definition.get("prompt_id")) != str(prompt_id):
                    continue
                if grid_cell_id is not None and str(definition.get("grid_cell_id")) != str(grid_cell_id):
                    continue
                if prompt_id is None and grid_cell_id is None:
                    continue
                intent = session.scalar(
                    select(models.FalSubmissionIntent)
                    .where(
                        models.FalSubmissionIntent.subject_id == subject.id,
                        models.FalSubmissionIntent.generation == subject.generation,
                    )
                    .limit(1)
                )
                if intent is None:
                    return None
                return {
                    "intent_id": intent.id,
                    "subject_id": subject.id,
                    "generation": intent.generation,
                    "state": intent.state,
                    "provider_request_id": intent.provider_request_id,
                    "submission_fence": intent.submission_fence,
                }
            return None

    def record_submission_intent(self, intent_id: str, request_id: str) -> None:
        with self.session_factory.begin() as session:
            intent = _required(session, models.FalSubmissionIntent, intent_id)
            if intent.provider_request_id not in {None, request_id}:
                raise ValueError("FAL provider request identity changed")
            if intent.state in {"completed", "failed", "ambiguous", "canceled"}:
                if intent.provider_request_id == request_id:
                    return
                raise ValueError("FAL submission intent is terminal")
            intent.provider_request_id = request_id
            intent.state = "submitted"
    def finish_submission_intent(
        self,
        eval_run_id: str,
        *,
        prompt_id: str | None = None,
        grid_cell_id: str | None = None,
        state: str = "completed",
    ) -> None:
        if state not in {"completed", "failed", "ambiguous", "canceled"}:
            raise ValueError("FAL submission terminal state is invalid")
        with self.session_factory.begin() as session:
            admission_id = session.scalar(
                select(models.FalAdmission.id).where(models.FalAdmission.eval_run_id == eval_run_id).limit(1)
            )
            if admission_id is None:
                return
            subjects = list(session.scalars(select(models.FalSubject).where(models.FalSubject.admission_id == admission_id)))
            for subject in subjects:
                definition = dict(subject.definition or {})
                if prompt_id is not None and str(definition.get("prompt_id")) != str(prompt_id):
                    continue
                if grid_cell_id is not None and str(definition.get("grid_cell_id")) != str(grid_cell_id):
                    continue
                if prompt_id is None and grid_cell_id is None:
                    continue
                intent = session.scalar(
                    select(models.FalSubmissionIntent)
                    .where(
                        models.FalSubmissionIntent.subject_id == subject.id,
                        models.FalSubmissionIntent.generation == subject.generation,
                    )
                    .limit(1)
                )
                if intent is not None and intent.state not in {"completed", "failed", "ambiguous", "canceled"}:
                    intent.state = state
                return

    def mark_failed(self, eval_run_id: str, message: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "failed"
            self._set_grid_status(session, run, "failed", error=message)

    def mark_succeeded(self, eval_run_id: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "succeeded"
            items, selected_ids = self._selected_grid_scope(session, run)
            for item in items:
                children = session.scalars(select(models.GenerationQueueChild).where(models.GenerationQueueChild.queue_item_id == item.id)).all()
                if children and all(child.state == "succeeded" for child in children):
                    item.state = "succeeded"
                elif any(child.state == "succeeded" for child in children):
                    item.state = "partial"
            self._aggregate_grid_status(session, selected_ids)

    def mark_canceled(self, eval_run_id: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "canceled"
            self._set_grid_status(session, run, "cancelled")

    @staticmethod
    def _selected_grid_scope(session: Session, run: models.EvalRun) -> tuple[list[models.GenerationQueueItem], set[str]]:
        items = [
            item for item in session.scalars(select(models.GenerationQueueItem)).all()
            if str((item.request_snapshot or {}).get("run_id")) == str(run.id)
        ]
        selected_ids = {
            str(entry["grid_cell_id"])
            for item in items
            for entry in ((item.request_snapshot or {}).get("cells") or [])
            if isinstance(entry, dict) and entry.get("grid_cell_id")
        }
        return items, selected_ids

    @staticmethod
    def _aggregate_grid_status(session: Session, selected_ids: set[str], terminal_state: str | None = None) -> None:
        if not selected_ids:
            return
        cells = session.scalars(select(models.GridCell).where(models.GridCell.id.in_(selected_ids))).all()
        by_grid: dict[str, list[models.GridCell]] = {}
        selected_by_grid: dict[str, list[models.GridCell]] = {}
        for cell in cells:
            by_grid.setdefault(cell.grid_definition_id, []).append(cell)
            selected_by_grid.setdefault(cell.grid_definition_id, []).append(cell)
        for grid_id in selected_by_grid:
            all_cells = session.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid_id)).all()
            grid = session.get(models.GridDefinition, grid_id)
            if all_cells and all(cell.status == "succeeded" and cell.eval_output_id for cell in all_cells):
                grid.status = "succeeded"
            elif terminal_state and all(cell.status == terminal_state for cell in selected_by_grid[grid_id]) and len(selected_by_grid[grid_id]) == len(all_cells):
                grid.status = terminal_state
            elif any(cell.status in {"succeeded", "failed", "cancelled"} for cell in selected_by_grid[grid_id]):
                grid.status = "partial"

    @staticmethod
    def _set_grid_status(session: Session, run: models.EvalRun, state: str, *, error: str | None = None) -> None:
        items, selected_ids = SQLAlchemyEvalOutputSink._selected_grid_scope(session, run)
        if not selected_ids:
            return
        for cell in session.scalars(select(models.GridCell).where(models.GridCell.id.in_(selected_ids), models.GridCell.status.notin_({"succeeded"}))).all():
            cell.status = state
            if error:
                cell.output_snapshot = {**dict(cell.output_snapshot or {}), "error": error}
        for item in items:
            for child in session.scalars(select(models.GenerationQueueChild).where(models.GenerationQueueChild.queue_item_id == item.id)).all():
                if child.state not in {"succeeded"}:
                    child.state = state
                    if error:
                        child.error = error
        SQLAlchemyEvalOutputSink._aggregate_grid_status(session, selected_ids, terminal_state=state)

    def link_grid_cell(self, grid_definition_id: str, output_id: str, x_index: int, y_index: int, grid_cell_id: str | None = None) -> None:
        with self.session_factory.begin() as session:
            cell = session.get(models.GridCell, grid_cell_id) if grid_cell_id else session.scalar(
                select(models.GridCell).where(
                    models.GridCell.grid_definition_id == grid_definition_id,
                    models.GridCell.x_index == x_index,
                    models.GridCell.y_index == y_index,
                ).order_by(models.GridCell.ordinal).limit(1)
            )
            if cell is None:
                cell = models.GridCell(
                    grid_definition_id=grid_definition_id,
                    eval_output_id=output_id,
                    ordinal=0,
                    x_index=x_index,
                    y_index=y_index,
                    coordinate={"x": x_index, "y": y_index, "z": None},
                    status="succeeded",
                )
                session.add(cell)
            else:
                cell.eval_output_id = output_id
                cell.status = "succeeded"
                cell.output_snapshot = {**dict(cell.output_snapshot or {}), "eval_output_id": output_id}
            queue_item_id = None
            for item in session.scalars(select(models.GenerationQueueItem)).all():
                snapshot_cells = list((item.request_snapshot or {}).get("cells") or [])
                if any(str(entry.get("grid_cell_id")) == str(cell.id) for entry in snapshot_cells if isinstance(entry, dict)):
                    queue_item_id = item.id
                    break
            if queue_item_id:
                children = session.scalars(select(models.GenerationQueueChild).where(models.GenerationQueueChild.queue_item_id == queue_item_id)).all()
                for child in children:
                    if str((child.result or {}).get("grid_cell_id")) == str(cell.id):
                        child.state = "succeeded"
                        child.result = {**dict(child.result or {}), "output_id": output_id}

    def grid_cell_exists(
        self,
        grid_definition_id: str,
        x_index: int,
        y_index: int,
        *,
        grid_cell_id: str | None = None,
        session: Session | None = None,
    ) -> bool:
        if session is not None:
            if grid_cell_id:
                return session.scalar(select(models.GridCell.id).where(models.GridCell.id == grid_cell_id, models.GridCell.grid_definition_id == grid_definition_id, models.GridCell.status == "succeeded", models.GridCell.eval_output_id.is_not(None)).limit(1)) is not None
            return session.scalar(
                select(models.GridCell.id).where(
                    models.GridCell.grid_definition_id == grid_definition_id,
                    models.GridCell.x_index == x_index,
                    models.GridCell.y_index == y_index,
                    models.GridCell.status == "succeeded",
                    models.GridCell.eval_output_id.is_not(None),
                ).limit(1)
            ) is not None
        with self.session_factory() as owned:
            return self.grid_cell_exists(grid_definition_id, x_index, y_index, grid_cell_id=grid_cell_id, session=owned)

    def _download(self, url: str, expected_size: int | None):
        from titles_api.storage.fal_artifacts import MAX_ARTIFACT_BYTES

        if url.startswith("data:"):
            header, separator, encoded = url.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError("FAL inline artifact must use base64 encoding")
            try:
                body = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("FAL inline artifact is invalid base64") from exc
            if len(body) > MAX_ARTIFACT_BYTES:
                raise ValueError("FAL inline artifact exceeds the configured byte limit")
            if expected_size is not None and expected_size != len(body):
                raise ValueError(f"FAL artifact size mismatch: expected {expected_size}, received {len(body)}")
            return self.cache.hydrate(len(body), lambda stream: stream.write(body))

        response = self.artifact_transport.request(
            "GET",
            url,
            max_bytes=MAX_ARTIFACT_BYTES,
            max_error_bytes=64 * 1024,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise ValueError(f"FAL artifact download failed with HTTP {response.status_code}")
        length = len(response.body)
        if expected_size is not None:
            if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
                raise ValueError("FAL artifact size is invalid")
            if expected_size != length:
                raise ValueError(f"FAL artifact size mismatch: expected {expected_size}, received {length}")
        return self.cache.hydrate(length, lambda stream: stream.write(response.body))


def backfill_eval_local_locations(session: Session) -> dict[str, int]:
    """Repair legacy FAL output locations after verifying their local bytes."""
    repaired = missing = skipped = 0
    rows = session.execute(
        select(models.AssetLocation, models.Asset, models.Project)
        .join(models.Asset, models.Asset.id == models.AssetLocation.asset_id)
        .join(models.Project, models.Project.id == models.Asset.project_id)
        .join(models.EvalOutput, models.EvalOutput.asset_id == models.Asset.id)
        .where(models.AssetLocation.provider == "local")
    ).all()
    for location, asset, project in rows:
        if location.verification_state == "available" and asset.content_blob_id:
            skipped += 1
            continue
        path = Path(location.uri)
        if not path.is_file():
            missing += 1
            continue
        digest_builder = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_builder.update(chunk)
                size += len(chunk)
        digest = digest_builder.hexdigest()
        StorageRepository(session, project.workspace_id).attach_verified_local_location(
            asset_id=asset.id,
            uri=location.uri,
            size=size,
            sha256=digest,
            mime_type=asset.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        repaired += 1
    session.flush()
    return {"repaired": repaired, "missing": missing, "skipped": skipped}


def _required(session: Session, model, object_id: str):
    value = session.get(model, object_id)
    if value is None:
        raise LookupError(f"{model.__name__} not found: {object_id}")
    return value
