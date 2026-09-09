from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("TITLES_WANDB_INGRESS_HOST", "127.0.0.1")
    port = int(os.environ.get("TITLES_WANDB_INGRESS_PORT", "8401"))
    if not 1 <= port <= 65535:
        raise SystemExit("TITLES_WANDB_INGRESS_PORT must be between 1 and 65535")
    uvicorn.run(
        "titles_api.wandb_ingress:create_wandb_ingress_app",
        factory=True,
        host=host,
        port=port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
