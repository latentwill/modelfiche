from __future__ import annotations

import os
import socket
from pathlib import Path

import uvicorn

from .app import create_app
from .security_middleware import LocalLaunchContract
from .settings import get_settings
from .storage.run_lease import RunLease


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("TITLES_API_PORT", "8400"))
    if not 1 <= port <= 65535:
        raise SystemExit("TITLES_API_PORT must be between 1 and 65535")
    with RunLease.acquire(
        Path(os.environ.get("TITLES_RUN_LOCK", "var/titles-dam.run.lock")).expanduser().resolve(),
        lease_id=os.environ.get("TITLES_RUN_LEASE_ID"),
    ) as lease:
        os.environ["TITLES_RUN_LEASE_ID"] = lease.lease_id
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(128)
            listener.set_inheritable(False)
            contract = LocalLaunchContract(
                allowed_hosts=frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}),
                allowed_origins=frozenset(get_settings().cors_origins),
                allow_uds=False,
            )
            config = uvicorn.Config(
                create_app(contract),
                host=host,
                port=port,
                proxy_headers=False,
                forwarded_allow_ips="",
                reload=False,
                access_log=True,
            )
            uvicorn.Server(config).run(sockets=[listener])
        finally:
            listener.close()


if __name__ == "__main__":
    main()
