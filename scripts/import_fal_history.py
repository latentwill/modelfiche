#!/usr/bin/env python3
"""Idempotently import expanded FAL request-history records into DAM evals."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
from typing import Any

from sqlalchemy import select

from titles_api import models
from titles_api.image_provenance import fal_generated_at, normalize_fal_image_metadata
from titles_api.asset_cache import AssetCache
from titles_api.database import build_engine
from titles_api.integrations.config import CacheSettings
from titles_api.settings import get_settings
from titles_worker.sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink


def _project_key(record: dict[str, Any]) -> tuple[str, str]:
    payload = record.get("json_input") or {}
    loras = payload.get("loras") or []
    lora = loras[0].get("path", "") if loras and isinstance(loras[0], dict) else ""
    evidence = f"{lora} {payload.get('prompt', '')}".lower()
    if "kzapata" in evidence:
        return "kzapata", "prompt_or_lora"
    if "vcribb" in evidence:
        return "vcribb", "prompt_or_lora"
    return "vcribb", "legacy_titlesxyz_identity_default"


def _load(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records.extend(item for item in payload.get("items", []) if isinstance(item, dict))
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", nargs="+", type=Path)
    parser.add_argument("--kzapata-project", required=True)
    parser.add_argument("--vcribb-project", required=True)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    cache = AssetCache(CacheSettings(root=settings.cache_root.resolve(), max_bytes=settings.cache_root_quota_bytes))
    sink = SQLAlchemyEvalOutputSink(factory, cache)
    project_ids = {"kzapata": args.kzapata_project, "vcribb": args.vcribb_project}
    records = _load(args.records)
    imported = skipped = 0

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        key, evidence = _project_key(record)
        record["_project_evidence"] = evidence
        groups.setdefault((project_ids[key], str(record.get("endpoint_id") or "fal-history")), []).append(record)

    for (project_id, endpoint), group in groups.items():
        with factory.begin() as session:
            project = session.get(models.Project, project_id)
            if project is None:
                raise LookupError(f"project not found: {project_id}")
            prompt_set = session.scalar(select(models.PromptSet).where(models.PromptSet.project_id == project_id, models.PromptSet.name == f"FAL history · {endpoint}"))
            if prompt_set is None:
                prompt_set = models.PromptSet(project_id=project_id, name=f"FAL history · {endpoint}")
                session.add(prompt_set)
                session.flush()
            prompts = {row.text: row for row in session.scalars(select(models.Prompt).where(models.Prompt.prompt_set_id == prompt_set.id))}
            for record in group:
                text = str((record.get("json_input") or {}).get("prompt") or (record.get("json_output") or {}).get("prompt") or "Historical FAL output")
                if text not in prompts:
                    prompt = models.Prompt(prompt_set_id=prompt_set.id, text=text, position=len(prompts), metadata_={"source": "fal_history"})
                    session.add(prompt)
                    session.flush()
                    prompts[text] = prompt
            definition = session.scalar(select(models.EvalDefinition).where(models.EvalDefinition.project_id == project_id, models.EvalDefinition.endpoint == endpoint, models.EvalDefinition.prompt_set_id == prompt_set.id))
            if definition is None:
                definition = models.EvalDefinition(project_id=project_id, name=f"FAL history · {endpoint}", endpoint=endpoint, prompt_set_id=prompt_set.id, parameters={"historical_import": True})
                session.add(definition)
                session.flush()
            run = session.scalar(select(models.EvalRun).where(models.EvalRun.definition_id == definition.id, models.EvalRun.parameters_snapshot["historical_import"].as_boolean().is_(True)))
            if run is None:
                run = models.EvalRun(definition_id=definition.id, status="succeeded", parameters_snapshot={"historical_import": True}, provider_job_ids=[])
                session.add(run)
                session.flush()
            prompt_ids = {text: prompt.id for text, prompt in prompts.items()}
            run_id = run.id

        pending: list[dict[str, Any]] = []
        for record in group:
            request_id = str(record.get("request_id") or "")
            with factory() as session:
                exists = session.scalar(select(models.Asset.id).where(models.Asset.metadata_["fal_request_id"].as_string() == request_id).limit(1))
            if exists:
                skipped += 1
                continue
            pending.append(record)

        original_download = sink._download

        def prefetch(record: dict[str, Any]):
            entries = {}
            for image in (record.get("json_output") or {}).get("images") or []:
                if isinstance(image, dict) and image.get("url"):
                    url = str(image["url"])
                    for attempt in range(4):
                        try:
                            entries[url] = original_download(url, image.get("file_size"))
                            break
                        except Exception:
                            if attempt == 3:
                                raise
                            time.sleep(0.5 * (attempt + 1))
            return str(record.get("request_id") or ""), entries

        prefetched: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(prefetch, record) for record in pending]
            for count, future in enumerate(as_completed(futures), 1):
                request_id, entries = future.result()
                prefetched[request_id] = entries
                if count % 25 == 0:
                    print(json.dumps({"downloaded": count, "pending_group": len(pending), "imported": imported, "skipped": skipped}), flush=True)

        for record in pending:
            request_id = str(record.get("request_id") or "")
            request = dict(record.get("json_input") or {})
            response = dict(record.get("json_output") or {})
            response.update({"_request_id": request_id, "_request_input": request, "_history": {"sent_at": record.get("sent_at"), "started_at": record.get("started_at"), "ended_at": record.get("ended_at"), "duration": record.get("duration"), "project_evidence": record["_project_evidence"]}})
            prompt_text = str(request.get("prompt") or response.get("prompt") or "Historical FAL output")
            entries = prefetched[request_id]
            sink._download = lambda url, _size, entries=entries: entries[url]
            sink.ingest_outputs(run_id, prompt_ids[prompt_text], response)
            with factory.begin() as session:
                asset = session.scalar(select(models.Asset).where(models.Asset.metadata_["provider_request_ids"].contains(request_id)).order_by(models.Asset.created_at.desc()).limit(1))
                if asset is not None:
                    metadata = {**dict(asset.metadata_ or {}), "fal_request_id": request_id, "fal_request_input": request, "project_evidence": record["_project_evidence"]}
                    generated_at = fal_generated_at({"_request_id": request_id, "_history": response.get("_history")})
                    if generated_at:
                        metadata["generated_at"] = generated_at.isoformat()
                        asset.created_at = generated_at
                    asset.metadata_ = {**metadata, **normalize_fal_image_metadata(metadata)}
            imported += 1
            if imported % 25 == 0:
                print(json.dumps({"imported": imported, "skipped": skipped, "total": len(records)}), flush=True)
        sink._download = original_download
    print(json.dumps({"imported": imported, "skipped": skipped, "total": len(records)}))


if __name__ == "__main__":
    main()
