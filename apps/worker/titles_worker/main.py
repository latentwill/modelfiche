from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def configure_storage_environment() -> None:
    """Keep worker-relative storage paths aligned with the application root."""
    local_root = os.getenv("TITLES_LOCAL_ROOT")
    if not local_root:
        return
    root = Path(local_root).expanduser().resolve()
    for name, child in {
        "TITLES_ASSET_ROOT": "assets",
        "TITLES_CACHE_ROOT": "cache",
        "TITLES_EXPORT_ROOT": "exports",
        "TITLES_CONFIG_ROOT": "config",
        "TITLES_WANDB_UPLOAD_ROOT": "wandb-uploads",
    }.items():
        os.environ.setdefault(name, str(root / child))


def main() -> None:
    configure_storage_environment()
    api_root = Path(__file__).resolve().parents[2] / "api"
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))
    from titles_api.database import SessionLocal
    from titles_api.asset_cache import AssetCache
    from titles_api.integrations.config import CacheSettings, FalSettings
    from titles_api.integrations.fal.client import FalQueueClient
    from titles_api.settings import get_settings

    from .fal_jobs import FalEvalBatchHandler, FalEvalHandler, FalGridBatchHandler
    from .fal_request_resolver import SQLAlchemyFalRequestResolver
    from .import_jobs import S3ImportHandler
    from .hydration_jobs import CheckpointHydrationHandler
    from .image_lineage_jobs import ImageLineageRepairHandler
    from .export_jobs import ExportPackageHandler
    from .runner import JobRunner
    from .sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink
    from .wandb_uploads import WandbUploadReconciler
    from .checkpoint_handoffs import (
        CheckpointHandoffReconciler,
        CheckpointManifestPoller,
    )
    from .run_maintenance import RunMaintenance

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    cache = AssetCache(CacheSettings(root=settings.cache_root.resolve()))
    handlers = {
        "s3.import": S3ImportHandler(SessionLocal, cache),
        "checkpoint.hydrate": CheckpointHydrationHandler(SessionLocal, cache),
        "export.package": ExportPackageHandler(
            SessionLocal,
            settings.export_root,
            (settings.asset_root, settings.cache_root),
        ),
        "image.repair_lineage": ImageLineageRepairHandler(SessionLocal, cache),
    }
    # Resolve credentials for every claimed FAL job so saves, replacements, and
    # environment changes take effect without restarting the worker.
    sink = SQLAlchemyEvalOutputSink(SessionLocal, cache)
    resolver = SQLAlchemyFalRequestResolver(SessionLocal)

    def fal_eval(context, payload):
        return FalEvalHandler(
            FalQueueClient(FalSettings.from_env()), sink, request_resolver=resolver
        )(context, payload)

    def fal_batch(context, payload):
        return FalEvalBatchHandler(
            SessionLocal, FalQueueClient(FalSettings.from_env()), sink, resolver
        )(context, payload)

    def fal_grid(context, payload):
        return FalGridBatchHandler(
            SessionLocal, FalQueueClient(FalSettings.from_env()), sink, resolver
        )(context, payload)

    handlers["fal.eval"] = fal_eval
    handlers["fal.eval_batch"] = fal_batch
    handlers["fal.eval_admission"] = fal_batch
    handlers["fal.grid_batch"] = fal_grid
    handlers["fal_eval"] = fal_batch
    handlers["fal.grid_cell"] = fal_eval
    upload_reconciler = WandbUploadReconciler(SessionLocal)
    checkpoint_poller = CheckpointManifestPoller(SessionLocal)
    checkpoint_reconciler = CheckpointHandoffReconciler(SessionLocal)
    run_maintenance = RunMaintenance(
        SessionLocal,
        stale_after_seconds=settings.wandb_stale_after_seconds,
    )
    JobRunner(
        SessionLocal,
        handlers,
        maintenance_handlers=(
            checkpoint_poller.run_once,
            upload_reconciler.run_once,
            checkpoint_reconciler.run_once,
            run_maintenance.interrupt_stale_runs,
        ),
    ).run_forever()


if __name__ == "__main__":
    main()
