from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


_STEP = re.compile(r"(?:step|checkpoint|ckpt)[-_]?(\d+)", re.IGNORECASE)
_STOP = False


def _stop(*_args: object) -> None:
    global _STOP
    _STOP = True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _step(path: Path) -> int:
    match = _STEP.search(path.stem)
    if match:
        return int(match.group(1))
    digits = re.findall(r"\d+", path.stem)
    if digits:
        return int(digits[-1])
    raise ValueError(
        f"checkpoint filename does not include a training step: {path.name}"
    )


def _client(destination: dict[str, Any], credentials: dict[str, Any]):
    access = os.getenv(str(credentials["access_key"]))
    secret = os.getenv(str(credentials["secret_key"]))
    session = os.getenv(str(credentials.get("session_token") or "")) or None
    if not access or not secret:
        raise RuntimeError(
            "training storage credentials are unavailable in the named environment variables"
        )
    return boto3.client(
        "s3",
        endpoint_url=destination.get("endpoint_url"),
        region_name=destination.get("region"),
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        aws_session_token=session,
        config=Config(
            s3={"addressing_style": destination.get("addressing_style", "auto")}
        ),
    )


def _generation(client: Any, bucket: str, prefix: str) -> int:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    values = []
    for item in response.get("Contents", []):
        name = str(item.get("Key", "")).removeprefix(prefix).removesuffix(".json")
        if name.isdigit():
            values.append(int(name))
    return max(values, default=0) + 1


def _sync_files(
    client: Any,
    *,
    bucket: str,
    output: Path,
    run_prefix: str,
    sync_sets: list[dict[str, Any]],
    stable_seconds: int,
) -> int:
    now = time.time()
    uploaded = 0
    for sync_set in sync_sets:
        local = output / str(sync_set["local"]).strip("/")
        if not local.is_dir():
            continue
        remote = str(sync_set["remote"]).strip("/")
        refresh = bool(sync_set.get("refresh"))
        for path in local.rglob("*"):
            if not path.is_file() or now - path.stat().st_mtime < stable_seconds:
                continue
            key = f"{run_prefix}{remote}/{path.relative_to(local).as_posix()}"
            existing: dict[str, Any] | None = None
            try:
                existing = client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {
                    "404",
                    "NoSuchKey",
                    "NotFound",
                }:
                    raise
            if refresh:
                digest = _sha256(path)
                metadata = existing.get("Metadata", {}) if existing else {}
                if metadata.get("sha256") == digest:
                    continue
                client.upload_file(
                    str(path),
                    bucket,
                    key,
                    ExtraArgs={"Metadata": {"sha256": digest}},
                )
                uploaded += 1
            elif existing is None:
                client.upload_file(str(path), bucket, key, ExtraArgs={})
                uploaded += 1
    return uploaded


def sync_once(manifest: dict[str, Any], *, stable_seconds: int) -> dict[str, Any]:
    if manifest.get("schema_version") != "modelfiche.training-launch.v1":
        raise ValueError("unsupported training launch manifest")
    backup = manifest["backup"]
    if backup.get("schema_version") != "modelfiche.run-backup.v1":
        raise ValueError("unsupported backup configuration")
    output = Path(str(backup["source_directory"])).resolve()
    destination = backup["destination"]
    bucket = str(destination["bucket"])
    run_prefix = str(destination["prefix"])
    client = _client(destination, backup["credential_environment"])
    synced = _sync_files(
        client, bucket=bucket, output=output, run_prefix=run_prefix,
        sync_sets=list(backup.get("sync_sets", [])), stable_seconds=stable_seconds,
    )
    checkpoint_root = output / "checkpoints"
    if not checkpoint_root.is_dir():
        return {"uploaded": synced, "checkpoint_count": 0, "message": "checkpoint directory is not present yet"}
    descriptors: list[dict[str, Any]] = []
    now = time.time()
    candidates = sorted(
        {*checkpoint_root.rglob("*.safetensors"), *checkpoint_root.rglob("*.ckpt")}
    )
    for path in candidates:
        stat = path.stat()
        if now - stat.st_mtime < stable_seconds:
            continue
        digest = _sha256(path)
        step = _step(path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name)
        key = f"{run_prefix}checkpoints/{step}-{digest[:16]}-{safe_name}"
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise
            head = None
        if head is None:
            client.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": digest, "step": str(step)}},
            )
            head = client.head_object(Bucket=bucket, Key=key)
        if int(head.get("ContentLength", -1)) != stat.st_size:
            raise RuntimeError(f"remote size verification failed for {path.name}")
        descriptors.append(
            {
                "object_key": key,
                "sha256": digest,
                "size": stat.st_size,
                "step": step,
                "name": path.stem,
            }
        )
    if not descriptors:
        return {
            "uploaded": 0,
            "checkpoint_count": 0,
            "message": "no stable checkpoints are ready",
        }
    handoff = backup["checkpoint_handoff"]
    manifest_prefix = str(handoff["manifest_prefix"])
    generation = _generation(client, bucket, manifest_prefix)
    body = {
        "schema_version": "modelfiche.checkpoint-manifest.v1",
        "run_id": manifest["run"]["id"],
        "generation": generation,
        "checkpoints": descriptors,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    manifest_key = f"{manifest_prefix}{generation}.json"
    receipt = client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=raw,
        ContentType="application/json",
        Metadata={"sha256": digest},
    )
    callback_body = json.dumps(
        {
            "schema_version": "modelfiche.checkpoint-manifest.v1",
            "generation": generation,
            "manifest_key": manifest_key,
            "manifest_sha256": digest,
            "manifest_etag": str(receipt.get("ETag", "")).strip('"') or None,
            "manifest_version_id": receipt.get("VersionId"),
            "checkpoint_count": len(descriptors),
        }
    ).encode("utf-8")
    callback_required = bool(handoff.get("callback_required", True))
    callback_status = "skipped"
    token = os.getenv("MODELFICHE_HANDOFF_TOKEN")
    if token:
        authorization = base64.b64encode(f"api:{token}".encode("ascii")).decode("ascii")
        request = urllib.request.Request(
            str(handoff["callback_url"]),
            data=callback_body,
            method="POST",
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in {200, 201}:
                    raise RuntimeError(
                        f"checkpoint callback failed with HTTP {response.status}"
                    )
            callback_status = "delivered"
        except (urllib.error.URLError, RuntimeError):
            if callback_required:
                raise
            callback_status = "unavailable"
    elif callback_required:
        raise RuntimeError(
            "MODELFICHE_HANDOFF_TOKEN is required for checkpoint handoff"
        )
    return {
        "uploaded": synced + len(descriptors),
        "checkpoint_count": len(descriptors),
        "generation": generation,
        "manifest_key": manifest_key,
        "manifest_sha256": digest,
        "callback_status": callback_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up stable trainer checkpoints and publish Modelfiche handoffs"
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--stable-seconds", type=int, default=60)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while not _STOP:
        result = sync_once(manifest, stable_seconds=max(1, args.stable_seconds))
        print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return
        for _ in range(max(1, args.interval)):
            if _STOP:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
