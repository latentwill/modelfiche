from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ObjectInfo


class ChangeKind(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REMOTE_MISSING = "remote_missing"


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    kind: ChangeKind
    key: str
    remote: ObjectInfo | None
    stored: StoredObject | None


def reconcile(remote: list[ObjectInfo], stored: list[StoredObject]) -> list[ReconciliationItem]:
    remote_by_key = {item.key: item for item in remote}
    stored_by_key = {item.key: item for item in stored}
    result: list[ReconciliationItem] = []
    for key in sorted(remote_by_key.keys() | stored_by_key.keys()):
        current = remote_by_key.get(key)
        previous = stored_by_key.get(key)
        if previous is None:
            kind = ChangeKind.NEW
        elif current is None:
            kind = ChangeKind.REMOTE_MISSING
        elif current.etag != previous.etag or current.size != previous.size:
            kind = ChangeKind.CHANGED
        else:
            kind = ChangeKind.UNCHANGED
        result.append(ReconciliationItem(kind, key, current, previous))
    return result
