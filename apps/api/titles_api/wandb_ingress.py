from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Callable, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import models
from .checkpoint_handoffs import (
    HandoffConflict,
    HandoffError,
    handoff_projection,
    record_checkpoint_handoff,
)
from .database import build_engine, upgrade_installed_database
from .settings import get_settings
from .training_metrics import append_run_event
from .wandb_auth import WandbPrincipal, authenticate_wandb_request
from .wandb_protocol import (
    ProtocolError,
    apply_summary,
    finish_run,
    handle_filestream,
    handle_graphql,
    validate_path_identity,
)
from .wandb_uploads import UploadError, capture_media_metadata


class IngressRateLimiter:
    def __init__(self, limit_per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if limit_per_minute <= 0:
            raise ValueError("limit_per_minute must be positive")
        self.limit = limit_per_minute
        self.clock = clock
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        now = self.clock()
        cutoff = now - 60
        with self._lock:
            requests = self._requests[key]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                return max(1, math.ceil(60 - (now - requests[0])))
            requests.append(now)
            return None


async def _bounded_json(request: Request, maximum: int) -> object:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared < 0 or declared > maximum:
            raise HTTPException(status_code=413, detail="W&B request body exceeds the ingress limit")
    body = await request.body()
    if len(body) > maximum:
        raise HTTPException(status_code=413, detail="W&B request body exceeds the ingress limit")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="W&B request body is not valid JSON") from exc


async def _bounded_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared < 0 or declared > maximum:
            raise HTTPException(status_code=413, detail="W&B upload exceeds the ingress limit")
    body = await request.body()
    if len(body) > maximum:
        raise HTTPException(status_code=413, detail="W&B upload exceeds the ingress limit")
    return body


def create_wandb_ingress_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    upload_root: Path | None = None,
    initialize_schema: bool = True,
) -> FastAPI:
    settings = get_settings()
    database_url = settings.wandb_ingress_database_url or settings.database_url
    if session_factory is None:
        engine = build_engine(database_url)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    resolved_upload_root = (upload_root or settings.wandb_upload_root).resolve()
    limiter = IngressRateLimiter(settings.wandb_ingress_rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_runtime_dirs()
        resolved_upload_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_upload_root.chmod(0o700)
        if initialize_schema:
            upgrade_installed_database(database_url)
        yield

    app = FastAPI(
        title="ModelFiche W&B Compatibility Ingress",
        version="wandb-0.28.0-ai-toolkit-v1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def db_provider() -> Iterator[Session]:
        assert session_factory is not None
        with session_factory() as session:
            yield session

    def principal_for(session: Session, authorization: str | None) -> WandbPrincipal:
        principal = authenticate_wandb_request(session, authorization)
        retry_after = limiter.retry_after(principal.credential.id)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="W&B ingress rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        return principal

    @app.get("/health")
    def health(db: Session = Depends(db_provider)):
        db.execute(select(models.TrainingRun.id).limit(1))
        return {"status": "ok", "protocol": "wandb-0.28.0-ai-toolkit-v1"}

    @app.post("/graphql")
    async def graphql(
        request: Request,
        db: Session = Depends(db_provider),
        authorization: str | None = Header(default=None),
    ):
        principal = principal_for(db, authorization)
        try:
            body = await _bounded_json(request, settings.wandb_graphql_max_bytes)
            data = handle_graphql(db, principal, body, base_url=str(request.base_url).rstrip("/"))
        except ProtocolError as exc:
            db.rollback()
            return JSONResponse(
                status_code=422,
                content={"errors": [{"message": str(exc), "extensions": {"code": "UNSUPPORTED_OPERATION"}}]},
            )
        except Exception:
            db.rollback()
            raise
        db.commit()
        return {"data": data}

    @app.post("/files/{entity}/{project}/{run_id}/file_stream")
    async def file_stream(
        entity: str,
        project: str,
        run_id: str,
        request: Request,
        db: Session = Depends(db_provider),
        authorization: str | None = Header(default=None),
    ):
        principal = principal_for(db, authorization)
        try:
            validate_path_identity(principal, entity, project, run_id)
            body = await _bounded_json(request, settings.wandb_filestream_max_bytes)
            result = handle_filestream(db, principal, body)
        except ProtocolError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            db.rollback()
            raise
        db.commit()
        return result

    @app.post("/checkpoint-handoffs/{run_id}")
    async def checkpoint_handoff(
        run_id: str,
        request: Request,
        response: Response,
        db: Session = Depends(db_provider),
        authorization: str | None = Header(default=None),
    ):
        principal = principal_for(db, authorization)
        body = await _bounded_json(request, settings.wandb_graphql_max_bytes)
        try:
            handoff, inserted = record_checkpoint_handoff(db, principal, run_id, body)
        except HandoffConflict as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HandoffError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            db.rollback()
            raise
        db.commit()
        response.status_code = 201 if inserted else 200
        return handoff_projection(handoff)


    @app.put("/wandb-upload/{upload_id}")
    async def upload(
        upload_id: str,
        request: Request,
        generation: int = Query(ge=0),
        db: Session = Depends(db_provider),
    ):
        upload_row = db.get(models.RunUpload, upload_id, with_for_update=True)
        if upload_row is None:
            raise HTTPException(status_code=404, detail="W&B upload instruction was not found")
        if upload_row.generation != generation:
            raise HTTPException(status_code=409, detail="W&B upload instruction generation is stale")
        maximum = (
            settings.wandb_image_max_bytes
            if upload_row.kind == "image"
            else settings.wandb_config_max_bytes
        )
        body = await _bounded_body(request, maximum)
        digest = hashlib.sha256(body).hexdigest()
        if upload_row.expected_size is not None and len(body) != upload_row.expected_size:
            raise HTTPException(status_code=409, detail="W&B upload size does not match its instruction")
        if upload_row.expected_sha256 is not None and digest != upload_row.expected_sha256:
            raise HTTPException(status_code=409, detail="W&B upload digest does not match its instruction")

        destination = resolved_upload_root / upload_row.run_id / upload_row.id
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_bytes(body)
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        upload_row.state = "uploaded"
        upload_row.expected_size = len(body)
        upload_row.expected_sha256 = digest
        upload_row.etag = digest
        upload_row.metadata_ = {
            **dict(upload_row.metadata_ or {}),
            "staged_path": str(destination),
            "content_type": request.headers.get("content-type"),
        }
        run = db.get(models.TrainingRun, upload_row.run_id)
        launch = db.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == upload_row.run_id))
        credential = db.get(models.WandbIngestCredential, launch.wandb_credential_id) if launch else None
        if run is None or launch is None or credential is None:
            db.rollback()
            raise HTTPException(status_code=409, detail="W&B upload run is unavailable")
        if upload_row.relative_path == "wandb-summary.json":
            try:
                summary = json.loads(body)
                if not isinstance(summary, dict):
                    raise ValueError("summary must be an object")
                capture_media_metadata(db, run, launch, summary)
                apply_summary(db, run, summary)
            except (UnicodeDecodeError, json.JSONDecodeError, UploadError, ValueError) as exc:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"invalid W&B summary upload: {exc}") from exc
        append_run_event(
            db,
            run,
            type="run.upload.received",
            idempotency_key=f"wandb:upload:{upload_row.id}:{generation}:{digest}",
            payload={"upload_id": upload_row.id, "relative_path": upload_row.relative_path, "size": len(body)},
            step=upload_row.step,
        )
        if upload_row.relative_path == "wandb-summary.json" and run.status == "running":
            finish_run(
                db,
                WandbPrincipal(credential=credential, launch=launch, run=run, claims={}),
                exit_code=0,
                signal="final-summary-upload",
            )
        db.commit()
        return Response(status_code=200)

    @app.get("/api/transfers/{transfer_id}/download")
    def training_export_download(transfer_id: str, db: Session = Depends(db_provider)):
        job = db.get(models.Job, transfer_id)
        if (
            job is None
            or job.kind != "export.package"
            or job.state != models.JobState.succeeded
            or not job.payload.get("training_launch")
        ):
            raise HTTPException(status_code=404, detail="training export not found")

        raw_path = job.result.get("path")
        if not raw_path:
            raise HTTPException(status_code=404, detail="training export is missing")
        root = settings.export_root.resolve()
        path = Path(str(raw_path)).resolve()
        if not (path == root or root in path.parents):
            raise HTTPException(status_code=403, detail="training export is outside the export root")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="training export is missing")
        return FileResponse(
            path,
            filename=job.result.get("filename") or path.name,
            media_type="application/zip",
        )

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unsupported(path: str):
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"unsupported W&B compatibility route: /{path}",
                "code": "UNSUPPORTED_WANDB_ROUTE",
            },
        )

    return app

