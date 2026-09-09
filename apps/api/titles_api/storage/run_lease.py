from __future__ import annotations

import fcntl
import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO


class RunLeaseBusy(RuntimeError):
    pass


@dataclass(slots=True)
class RunLease:
    path: Path
    lease_id: str
    _stream: IO[str]

    @classmethod
    def acquire(cls, path: Path, *, lease_id: str | None = None) -> "RunLease":
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise RunLeaseBusy(f"another Titles runtime owns {path}") from exc
        owned_lease_id = lease_id or str(uuid.uuid4())
        stream.seek(0)
        stream.truncate()
        json.dump(
            {"lease_id": owned_lease_id, "pid": os.getpid(), "host": socket.gethostname()},
            stream,
            sort_keys=True,
        )
        stream.flush()
        os.fsync(stream.fileno())
        return cls(path=path, lease_id=owned_lease_id, _stream=stream)

    def release(self) -> None:
        if self._stream.closed:
            return
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.flush()
        os.fsync(self._stream.fileno())
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()

    def __enter__(self) -> "RunLease":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
