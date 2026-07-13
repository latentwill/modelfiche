# Modelfiche

Local-first digital asset management for image-model datasets, training runs,
checkpoints, evaluations, grids, reviews, and model promotion. The current
release is an operator tool: it has local attributed profiles but no auth.

## Runtime

- FastAPI and SQLite API on `http://127.0.0.1:8400`
- Durable polling worker for S3 imports, checkpoint hydration, exports, and FAL jobs
- Bare React operator UI on `http://127.0.0.1:5173`
- `titles` CLI, designed for both interactive and agentic use
- Optional W&B 0.28.0 compatibility ingress on `http://127.0.0.1:8401`

## Install

```bash
uv sync --extra dev
pnpm --dir apps/web install
uv run alembic upgrade head
```

## S3 configuration

Import sources are standard S3 connections. For AWS, omit the endpoint and let
boto3 use its normal credential chain. For another S3-compatible service, set
an endpoint and choose `auto`, `path`, or `virtual` addressing.

Credentials are never saved to SQLite. Each source stores a credential prefix,
such as `TRAINING`; the process reads `TRAINING_ACCESS_KEY`,
`TRAINING_SECRET_KEY`, and optional `TRAINING_SESSION_TOKEN`. The default `S3`
prefix also supports `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and the rest
of boto3's AWS credential chain.

The existing Mega source can be injected from 1Password:

```bash
cp .env.1password.example .env.1password
op run --env-file=.env.1password -- uv run titles server start
```

Create a connector from the CLI:

```bash
uv run titles s3 source-create \
  --name training-runs \
  --bucket my-training-bucket \
  --region us-east-1 \
  --allowed-prefix completed-runs/ \
  --credential-env-prefix TRAINING
```

Add `--endpoint-url https://s3.example.com --addressing-style path` for a
custom provider. `titles s3 sources` returns the UUID required by browse and
import commands.

## Remote image serving

MEGA S4 can be used as an S3-compatible origin for imported image assets. Titles
stores the S3 object identity and serves images through the typed delivery
descriptor; it does not need to download the original into `assets/` first.
Direct browser delivery requires a current exact object version. If the object
only supports conditional reads, Titles uses a bounded proxy and verifies the
bytes before serving them. Thumbnails are derivative cache entries, not durable
copies of the original.

This remote-origin path applies to imported assets. FAL-generated outputs still
use the worker's local publication path until an S3 write-ready output policy is
selected and admitted.

## Run

Start the API, worker, and frontend together:

```bash
uv run titles server start
uv run titles server status
```

`server start` waits for the API and frontend health checks, then supervises
all three processes. If any process exits, the supervisor stops the remaining
services and `server status` reports failure. The frontend runs at
`http://127.0.0.1:5173`; the API runs at `http://127.0.0.1:8400`.

Stop the complete service stack with:

```bash
uv run titles server stop
```

Logs are written to `var/server.log`, `var/worker.log`, `var/web.log`, and
`var/supervisor.log`.

## AI Toolkit live logging

Modelfiche exposes a narrow W&B 0.28.0 compatibility service for AI Toolkit.
It accepts run start/resume, scalar and text metrics, image samples with
captions, summaries, and completion. The regular API pre-creates the expected
run and the trainer authenticates with a run-scoped signed token.

Generate a local signing key, register only its public key, create a launch,
and render the trainer environment:

```bash
uv run titles --json wandb key generate \
  --private-key-output ~/.config/modelfiche/ai-toolkit-signing.key
uv run titles --json wandb credential register \
  --alias ai-toolkit --project-id <project-id> --public-key <public-key>
uv run titles --json training-launch create --input launch.json
uv run titles --json training-launch agent-environment <launch-id> \
  --private-key-file ~/.config/modelfiche/ai-toolkit-signing.key \
  --output /run/secrets/modelfiche-training.env
```

Use `training-launch inspect`, `training-launch wait`, `run live`, and
`run metrics` for machine-readable agent monitoring. See
[docs/WANDB_COMPATIBILITY.md](docs/WANDB_COMPATIBILITY.md) for deployment,
reverse-proxy routing, launch JSON, key handling, failure recovery, and the
pinned compatibility-test procedure.

## Agentic CLI

All commands accept `--json`; collection commands also support `--jsonl`.
Mutations accept explicit IDs, the active local profile is sent for attribution,
and long operations expose durable jobs that can be polled with
`titles job wait <job-id>`.

```bash
uv run titles --json profile list
uv run titles --json project list
uv run titles --json s3 sources
uv run titles --json s3 ls --source <source-uuid> --prefix completed-runs/
uv run titles --json import create --source <source-uuid> \
  --project <project-uuid> --prefix completed-runs/example/ --wait
```

Use `uv run titles --help` for the full command tree.

## Operator APIs

The wireframe-facing application contracts are available under `/api`:

- `GET /dashboard` and `GET /search?q=...` provide the workspace shell data.
- `POST /checkpoints/{id}/hydrate` queues an atomic, verified S3 download.
- `/transfers` creates and tracks local export ZIPs; completed exports are
  downloaded from `/transfers/{id}/download`.
- `/operator-settings` stores nonsecret local defaults. The
  `/operator-settings/test/{llm|fal|storage}` checks are configuration-only and
  never call a provider. Credential values remain process environment variables.
- `POST /prompt-sets/generate` is the explicit LLM action used by the editable
  prompt-set builder. It reads the configured provider/model, accepts
  `instruction`, `count`, and structured `context`, then returns ordered
  `{id, prompt, position, metadata}` objects with `persisted: false`. Saving the
  edited result remains a separate `POST /prompt-sets` action.

Export packages contain a JSON manifest, selected asset metadata,
reviews/comments, and any selected files that are already hydrated locally.

## Verify

```bash
uv run pytest -q
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
```

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md),
[CLI_SPEC.md](CLI_SPEC.md), [FAL_ENDPOINT_SCHEMAS.md](FAL_ENDPOINT_SCHEMAS.md),
and [docs/WANDB_COMPATIBILITY.md](docs/WANDB_COMPATIBILITY.md) for the domain,
CLI, provider, and W&B compatibility contracts.
