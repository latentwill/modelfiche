from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    etag: str
    modified_at: datetime | None = None
    storage_class: str | None = None

    @property
    def name(self) -> str:
        return self.key.rstrip("/").rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        name = self.name.lower()
        return "." + name.rsplit(".", 1)[-1] if "." in name else ""


@dataclass(frozen=True, slots=True)
class BrowsePage:
    prefix: str
    prefixes: tuple[str, ...] = ()
    objects: tuple[ObjectInfo, ...] = ()
    next_cursor: str | None = None
    is_truncated: bool = False


@dataclass(slots=True)
class Inventory:
    prefix: str
    objects: list[ObjectInfo] = field(default_factory=list)
    history_count: int = 0

    def current(self) -> list[ObjectInfo]:
        marker = self.prefix.rstrip("/") + "/_history/"
        return [item for item in self.objects if not item.key.startswith(marker)]
