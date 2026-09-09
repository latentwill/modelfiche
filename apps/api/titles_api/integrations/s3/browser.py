from __future__ import annotations

from collections.abc import Iterator
from datetime import timezone
from typing import Any, BinaryIO

from ..config import S3Settings
from titles_api.storage.s3_client import create_source_s3_client
from .models import BrowsePage, Inventory, ObjectInfo


class PrefixAccessError(ValueError):
    pass


def normalize_prefix(prefix: str) -> str:
    parts = []
    for part in prefix.strip().lstrip("/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PrefixAccessError("parent traversal is not allowed")
        parts.append(part)
    return "/".join(parts) + ("/" if prefix.endswith("/") and parts else "")


class S3Browser:
    """A read-only S3 browser scoped to a configured bucket.

    The injected client only needs boto3's list/get/head methods, which keeps this
    module straightforward to test with botocore Stubber or a small fake.
    """

    def __init__(self, client: Any, settings: S3Settings):
        self.client = client
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: S3Settings) -> "S3Browser":
        return cls(create_source_s3_client(settings), settings)

    def require_allowed(self, prefix: str) -> str:
        safe = normalize_prefix(prefix)
        configured = tuple(normalize_prefix(value) for value in self.settings.allowed_prefixes)
        if configured and not any(
            safe.rstrip("/") == root.rstrip("/") or safe.startswith(root.rstrip("/") + "/")
            for root in configured
        ):
            raise PrefixAccessError("prefix is outside configured allowed prefixes")
        return safe
    def browse(self, prefix: str, *, cursor: str | None = None, page_size: int = 200) -> BrowsePage:
        safe = self.require_allowed(prefix)
        params: dict[str, Any] = {
            "Bucket": self.settings.bucket,
            "Prefix": safe,
            "Delimiter": "/",
            "MaxKeys": min(max(page_size, 1), 1000),
        }
        if cursor:
            params["ContinuationToken"] = cursor
        response = self.client.list_objects_v2(**params)
        prefixes = tuple(item["Prefix"] for item in response.get("CommonPrefixes", ()))
        objects = tuple(self._object(item) for item in response.get("Contents", ()) if item["Key"] != safe)
        return BrowsePage(
            prefix=safe,
            prefixes=prefixes,
            objects=objects,
            next_cursor=response.get("NextContinuationToken"),
            is_truncated=bool(response.get("IsTruncated")),
        )

    def walk(self, prefix: str, *, include_history: bool = False) -> Iterator[ObjectInfo]:
        safe = self.require_allowed(prefix)
        cursor: str | None = None
        history = safe.rstrip("/") + "/_history/"
        while True:
            params: dict[str, Any] = {"Bucket": self.settings.bucket, "Prefix": safe, "MaxKeys": 1000}
            if cursor:
                params["ContinuationToken"] = cursor
            response = self.client.list_objects_v2(**params)
            for raw in response.get("Contents", ()):
                item = self._object(raw)
                if include_history or not item.key.startswith(history):
                    yield item
            cursor = response.get("NextContinuationToken")
            if not cursor:
                break

    def inventory(self, prefix: str) -> Inventory:
        safe = self.require_allowed(prefix)
        inventory = Inventory(prefix=safe)
        history = safe.rstrip("/") + "/_history/"
        for obj in self.walk(safe, include_history=True):
            inventory.objects.append(obj)
            if obj.key.startswith(history):
                inventory.history_count += 1
        return inventory

    def read_small(self, key: str, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        safe = self.require_allowed(key)
        head = self.client.head_object(Bucket=self.settings.bucket, Key=safe)
        size = int(head.get("ContentLength", 0))
        if size > max_bytes:
            raise ValueError(f"object is too large for metadata read ({size} bytes)")
        response = self.client.get_object(Bucket=self.settings.bucket, Key=safe)
        body = response["Body"]
        try:
            data = body.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("object exceeded metadata read limit")
            return data
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def download(self, key: str, target: BinaryIO, *, etag: str | None = None) -> int:
        """Stream an allowed object into a caller-owned binary target."""
        safe = self.require_allowed(key)
        request: dict[str, Any] = {"Bucket": self.settings.bucket, "Key": safe}
        if etag:
            request["IfMatch"] = etag
        response = self.client.get_object(**request)
        body = response["Body"]
        response_etag = response.get("ETag")
        if etag and response_etag and str(response_etag).strip('"') != etag.strip('"'):
            close = getattr(body, "close", None)
            if close:
                close()
            raise ValueError("S3 object ETag changed before its body was read")
        written = 0
        try:
            while chunk := body.read(1024 * 1024):
                target.write(chunk)
                written += len(chunk)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        return written

    @staticmethod
    def _object(raw: dict[str, Any]) -> ObjectInfo:
        modified = raw.get("LastModified")
        if modified and modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        return ObjectInfo(
            key=raw["Key"],
            size=int(raw.get("Size", 0)),
            etag=str(raw.get("ETag", "")).strip('"'),
            modified_at=modified,
            storage_class=raw.get("StorageClass"),
        )
