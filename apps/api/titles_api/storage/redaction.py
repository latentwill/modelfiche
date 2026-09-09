from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_REDACTED = "[redacted]"
_CYCLE = "[redacted-cycle]"
_DEPTH = "[redacted-depth]"
_SECRET_KEY = re.compile(
    r"(?:authorization|password|secret|session[_-]?token|access[_-]?key|api[_-]?key|credential|cookie|signature)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(?:authorization|password|secret|token|access[_-]?key|api[_-]?key)\s*[=:]\s*[^\s,;]+"
)


def redact_for_persistence(value: Any, *, max_depth: int = 32) -> Any:
    """Return a bounded, recursively redacted value safe for durable generic fields."""
    return _redact(value, depth=0, max_depth=max_depth, stack=set())


def _redact(value: Any, *, depth: int, max_depth: int, stack: set[int]) -> Any:
    if depth > max_depth:
        return _DEPTH
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in stack:
            return _CYCLE
        stack.add(identity)
        try:
            return {
                str(key): _REDACTED if _SECRET_KEY.search(str(key)) else _redact(item, depth=depth + 1, max_depth=max_depth, stack=stack)
                for key, item in value.items()
            }
        finally:
            stack.remove(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in stack:
            return _CYCLE
        stack.add(identity)
        try:
            return [_redact(item, depth=depth + 1, max_depth=max_depth, stack=stack) for item in value]
        finally:
            stack.remove(identity)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        if parsed.username or parsed.password:
            return "[redacted-url]"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))
    value = _BEARER.sub(_REDACTED, value)
    return _ASSIGNMENT_SECRET.sub(_REDACTED, value)
