from __future__ import annotations

import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from .app import create_app, initialize_database
from .security_middleware import LocalLaunchContract, RemoteAccessContract
from .settings import get_settings
from .storage.run_lease import RunLease


def _remote_access_contract() -> RemoteAccessContract | None:
    origin = os.environ.get("TITLES_REMOTE_ACCESS_ORIGIN", "").strip()
    client_id = os.environ.get("TITLES_REMOTE_ACCESS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TITLES_REMOTE_ACCESS_CLIENT_SECRET", "").strip()
    configured = (origin, client_id, client_secret)
    if not any(configured):
        return None
    if not all(configured):
        raise SystemExit(
            "TITLES_REMOTE_ACCESS_ORIGIN, TITLES_REMOTE_ACCESS_CLIENT_ID, and "
            "TITLES_REMOTE_ACCESS_CLIENT_SECRET must be configured together"
        )
    return RemoteAccessContract(
        host=urlsplit(origin).netloc.lower(),
        origin=origin,
        client_id=client_id,
        client_secret=client_secret,
    )


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("TITLES_API_PORT", "8400"))
    if not 1 <= port <= 65535:
        raise SystemExit("TITLES_API_PORT must be between 1 and 65535")
    with RunLease.acquire(
        Path(os.environ.get("TITLES_RUN_LOCK", "var/titles-dam.run.lock"))
        .expanduser()
        .resolve(),
        lease_id=os.environ.get("TITLES_RUN_LEASE_ID"),
    ) as lease:
        os.environ["TITLES_RUN_LEASE_ID"] = lease.lease_id
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Complete the synchronous migration/seed pass before handing the
            # process to uvicorn. Running it through ASGI lifespan can leave
            # uvicorn waiting indefinitely when another local process has an
            # open SQLite WAL reader during large imports.
            initialize_database()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(128)
            listener.set_inheritable(False)
            contract = LocalLaunchContract(
                allowed_hosts=frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}),
                allowed_origins=frozenset(get_settings().cors_origins),
                allow_uds=False,
                remote_access=_remote_access_contract(),
            )
            config = uvicorn.Config(
                create_app(contract),
                host=host,
                port=port,
                proxy_headers=False,
                forwarded_allow_ips="",
                reload=False,
                access_log=True,
                lifespan="off",
            )
            uvicorn.Server(config).run(sockets=[listener])
        finally:
            listener.close()


if __name__ == "__main__":
    main()
