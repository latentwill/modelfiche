from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def main() -> None:
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
    from .export_jobs import ExportPackageHandler
    from .runner import JobRunner
    from .sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    cache = AssetCache(CacheSettings(root=settings.cache_root.resolve()))
    handlers = {
        "s3.import": S3ImportHandler(SessionLocal, cache),
        "checkpoint.hydrate": CheckpointHydrationHandler(SessionLocal, cache),
        "export.package": ExportPackageHandler(SessionLocal, settings.export_root, (settings.asset_root, settings.cache_root)),
    }
    if os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"):
        client = FalQueueClient(FalSettings.from_env())
        sink = SQLAlchemyEvalOutputSink(SessionLocal, cache)
        resolver = SQLAlchemyFalRequestResolver(SessionLocal)
        handlers["fal.eval"] = FalEvalHandler(client, sink, request_resolver=resolver)
        handlers["fal.eval_batch"] = FalEvalBatchHandler(SessionLocal, client, sink, resolver)
        handlers["fal.grid_batch"] = FalGridBatchHandler(SessionLocal, client, sink, resolver)
        handlers["fal_eval"] = handlers["fal.eval_batch"]
        handlers["fal.grid_cell"] = handlers["fal.eval"]
    JobRunner(SessionLocal, handlers).run_forever()


if __name__ == "__main__":
    main()
