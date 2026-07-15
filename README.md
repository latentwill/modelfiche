# Modelfiche

Local-first digital asset management for image-model datasets, training runs,
checkpoints, evaluations, grids, reviews, and model promotion. The current
release is an operator tool: it has local attributed profiles but no auth.

## Runtime

- Self-contained macOS app serving the API and React UI on `http://127.0.0.1:8400`
- Durable polling worker for S3 imports, checkpoint hydration, exports, and FAL jobs
- Embedded `mfiche` CLI for interactive and agentic use
- W&B 0.28.0 compatibility ingress on `http://127.0.0.1:8401`

## Install

### macOS

Download `Modelfiche-<version>-macos-<architecture>.dmg`, drag Modelfiche to
Applications, and open it. The app includes Python, the API, worker, W&B
ingress, web UI, migrations, and the agent CLI. Runtime data is stored under
`~/Library/Application Support/Modelfiche`; logs are under
`~/Library/Logs/Modelfiche`.

The embedded CLI is available at:

```bash
"/Applications/Modelfiche.app/Contents/MacOS/mfiche" --json training doctor
```

Release builds are reproducible from a development checkout:

```bash
scripts/build_macos_app.sh
```

Versioned ZIP, DMG, and SHA-256 files are written to `dist/macos/release/`.

### Development

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
op run --env-file=.env.1password -- uv run mfiche server start
```

Create a connector from the CLI:

```bash
uv run mfiche s3 source-create \
  --name training-runs \
  --bucket my-training-bucket \
  --region us-east-1 \
  --allowed-prefix completed-runs/ \
  --credential-env-prefix TRAINING
```

Add `--endpoint-url https://s3.example.com --addressing-style path` for a
custom provider. `mfiche s3 sources` returns the UUID required by browse and
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
uv run mfiche server start
uv run mfiche server status
```

`server start` waits for the API and frontend health checks, then supervises
all three processes. If any process exits, the supervisor stops the remaining
services and `server status` reports failure. The frontend runs at
`http://127.0.0.1:5173`; the API runs at `http://127.0.0.1:8400`.

Stop the complete service stack with:

```bash
uv run mfiche server stop
```

Logs are written to `var/server.log`, `var/worker.log`, `var/web.log`, and
`var/supervisor.log`.

## AI Toolkit live logging

Modelfiche exposes a narrow W&B 0.28.0 compatibility service for AI Toolkit.
It accepts run start/resume, scalar and text metrics, image samples with
captions, summaries, and completion. The regular API pre-creates the expected
run and the trainer authenticates with a run-scoped signed token.

Set up the local asymmetric signing identity once, inspect the actionable
readiness report, then start from a published dataset in the UI:

```bash
"/Applications/Modelfiche.app/Contents/MacOS/mfiche" --json training setup
"/Applications/Modelfiche.app/Contents/MacOS/mfiche" --json training doctor
```

After creating the launch in the UI, the launch screen supplies the exact
`training run prepare` command. It refuses to generate a trainer packet until
the immutable dataset export, signing identity, verified training storage, and
public HTTPS ingress all pass preflight. The resulting checksummed packet
contains the redacted manifest, AI Toolkit logging fragment, signed
run-scoped environment, and checkpoint-sync command.

Run `python3 modelfiche-run-sync.py --manifest manifest.json` from the packet
beside AI Toolkit on the trainer. It uploads only stable checkpoint files,
verifies the remote object,
publishes the generation manifest last, and notifies the authenticated
ModelFiche handoff endpoint. See
[docs/WANDB_COMPATIBILITY.md](docs/WANDB_COMPATIBILITY.md) for deployment,
reverse-proxy routing, launch JSON, key handling, failure recovery, and the
pinned compatibility-test procedure.

## Agentic CLI

All commands accept `--json`; collection commands also support `--jsonl`.
Mutations accept explicit IDs, the active local profile is sent for attribution,
and long operations expose durable jobs that can be polled with
`mfiche job wait <job-id>`.

```bash
uv run mfiche --json profile list
uv run mfiche --json project list
uv run mfiche --json s3 sources
uv run mfiche --json s3 ls --source <source-uuid> --prefix completed-runs/
uv run mfiche --json import create --source <source-uuid> \
  --project <project-uuid> --prefix completed-runs/example/ --wait
```

Use `uv run mfiche --help` for the full command tree.

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
