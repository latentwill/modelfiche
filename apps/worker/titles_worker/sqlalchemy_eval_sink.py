from __future__ import annotations

import mimetypes
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache


class SQLAlchemyEvalOutputSink:
    def __init__(self, session_factory: sessionmaker[Session], cache: AssetCache):
        self.session_factory = session_factory
        self.cache = cache

    def provider_submitted(self, eval_run_id: str, request_id: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            if request_id not in run.provider_job_ids:
                run.provider_job_ids = [*run.provider_job_ids, request_id]
            run.status = "running"

    def ingest_outputs(self, eval_run_id: str, prompt_id: str | None, response: dict[str, Any]) -> dict[str, Any]:
        images = response.get("images") or []
        if not isinstance(images, list):
            raise ValueError("FAL result images must be a list")
        with self.session_factory() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            definition = _required(session, models.EvalDefinition, run.definition_id)
            project = _required(session, models.Project, definition.project_id)
            output_count = 0
            output_ids: list[str] = []
            for index, image in enumerate(images):
                if not isinstance(image, dict) or not image.get("url"):
                    continue
                entry = self._download(str(image["url"]), image.get("file_size"))
                filename = Path(urlsplit(str(image["url"])).path).name or f"fal-output-{index}.png"
                asset = models.Asset(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    kind=models.AssetKind.image,
                    name=filename,
                    mime_type=image.get("content_type") or mimetypes.guess_type(filename)[0],
                    sha256=entry.sha256,
                    metadata_={"category": "eval_output", "provider": "fal"},
                )
                session.add(asset)
                session.flush()
                session.add(
                    models.AssetLocation(
                        asset_id=asset.id,
                        provider="local",
                        uri=str(entry.path),
                        size=entry.size,
                        hydration_state="hydrated",
                    )
                )
                width, height = image.get("width"), image.get("height")
                if width or height:
                    session.add(models.ImageMetadata(asset_id=asset.id, width=width, height=height))
                output = models.EvalOutput(
                    eval_run_id=run.id,
                    prompt_id=prompt_id,
                    asset_id=asset.id,
                    seed=response.get("seed"),
                    provider_metadata={key: value for key, value in image.items() if key not in {"url"}},
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

    def mark_failed(self, eval_run_id: str, message: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "failed"

    def mark_succeeded(self, eval_run_id: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "succeeded"

    def mark_canceled(self, eval_run_id: str) -> None:
        with self.session_factory.begin() as session:
            run = _required(session, models.EvalRun, eval_run_id)
            run.status = "canceled"

    def link_grid_cell(self, grid_definition_id: str, output_id: str, x_index: int, y_index: int) -> None:
        with self.session_factory.begin() as session:
            if self.grid_cell_exists(grid_definition_id, x_index, y_index, session=session):
                return
            session.add(
                models.GridCell(
                    grid_definition_id=grid_definition_id,
                    eval_output_id=output_id,
                    x_index=x_index,
                    y_index=y_index,
                )
            )

    def grid_cell_exists(
        self,
        grid_definition_id: str,
        x_index: int,
        y_index: int,
        *,
        session: Session | None = None,
    ) -> bool:
        if session is not None:
            return session.scalar(
                select(models.GridCell.id).where(
                    models.GridCell.grid_definition_id == grid_definition_id,
                    models.GridCell.x_index == x_index,
                    models.GridCell.y_index == y_index,
                ).limit(1)
            ) is not None
        with self.session_factory() as owned:
            return self.grid_cell_exists(grid_definition_id, x_index, y_index, session=owned)

    def _download(self, url: str, expected_size: int | None):
        with urllib.request.urlopen(url, timeout=120) as response:
            length = expected_size or int(response.headers.get("Content-Length") or 0)
            if length <= 0:
                data = response.read()
                length = len(data)
                return self.cache.hydrate(length, lambda stream: stream.write(data))
            return self.cache.hydrate(length, lambda stream: _copy(response, stream))


def _copy(source, target) -> None:
    while chunk := source.read(1024 * 1024):
        target.write(chunk)


def _required(session: Session, model, object_id: str):
    value = session.get(model, object_id)
    if value is None:
        raise LookupError(f"{model.__name__} not found: {object_id}")
    return value
