# Modelfiche

Modelfiche is a local-first workbench for image-model datasets, training runs,
checkpoints, evaluations, grids, reviews, and model promotion. It combines a
macOS application, a browser UI, a durable worker, and the `mfiche` automation
CLI in one operator-owned runtime.

> **Release scope:** the current release is a single-operator tool. Profiles
> provide attribution, not authentication. Optional remote access uses one
> full-authority shared credential; it is not a multi-user deployment. The
> macOS artifacts are currently unsigned and intended for controlled
> distribution.

## What it does

- Imports datasets and training runs from local folders or S3-compatible storage.
- Preserves typed lineage between source assets, dataset versions, runs,
  checkpoints, evaluations, grids, reviews, and promoted model versions.
- Serves remote originals through verified delivery descriptors and bounded
  proxies without requiring every asset to be hydrated locally.
- Submits Image, Eval, and Grid generation through server-side FAL admission
  and configured provider limits, without billing acknowledgements.
- Builds checksummed AI Toolkit trainer packets and receives checkpoints through
  outbound object storage; no inbound trainer tunnel is required.
- Exposes the same meaningful operator workflows through the web UI and the
  JSON-first `mfiche` CLI.
- Creates verified local backups and supports atomic restore and interrupted
  restore recovery.

## Documentation

| Guide | Use it for |
| --- | --- |
| [Documentation index](docs/README.md) | All operator, automation, architecture, and compatibility guides |
| [CLI guide](docs/CLI.md) | Installation, command discovery, common workflows, output, and exit codes |
| [Agent guide](docs/AGENT_GUIDE.md) | Deterministic automation rules and copy-ready recipes for coding agents |
| [W&B / AI Toolkit guide](docs/WANDB_COMPATIBILITY.md) | Trainer packets, optional live telemetry, signing, and checkpoint handoff |
| [CLI contract](CLI_SPEC.md) | Stable command behavior, safety, and compatibility requirements |
| [Product definition](PRODUCT.md) | Domain, users, product principles, and interaction constraints |
| [Contributing](CONTRIBUTING.md) | Development setup, tests, migrations, and pull-request expectations |
| [Security](SECURITY.md) | Supported deployment boundary and vulnerability reporting |

The application also includes an offline-friendly **Docs** screen with the
common operator and agent workflows.

## Install

### macOS application

Download `Modelfiche-<version>-macos-<architecture>.dmg`, drag Modelfiche into
Applications, and open it. The bundle contains Python, the API, migrations,
worker, web UI, optional W&B-compatible ingress, and the agent CLI.

Runtime data:

```text
~/Library/Application Support/Modelfiche
```

Logs:

```text
~/Library/Logs/Modelfiche
```

Run the embedded CLI directly:

```bash
"/Applications/Modelfiche.app/Contents/MacOS/mfiche" --json doctor
```

The Settings screen can install the packaged command into
`~/.local/bin/mfiche` without administrator access.

### Development checkout

Requirements: Python 3.11+, `uv`, Node.js, and `pnpm`.

```bash
uv sync --extra dev --extra compat-wandb
pnpm --dir apps/web install
uv run alembic upgrade head
uv tool install --editable .
```

Start the complete local stack:

```bash
mfiche start
mfiche status
```

Open `http://127.0.0.1:5173`, then stop the stack with:

```bash
mfiche stop
```

Without an installed entry point, replace `mfiche ...` with
`uv run mfiche ...`.

### Authenticated remote access

The API remains bound to loopback. To terminate an HTTPS Cloudflare Tunnel on
the Modelfiche host without exposing the no-auth local boundary, set all three
values in `.env.1password`:

```bash
TITLES_REMOTE_ACCESS_ORIGIN=https://modelfiche.example.com
TITLES_REMOTE_ACCESS_CLIENT_ID=op://Vault/Modelfiche Remote Access/client id
TITLES_REMOTE_ACCESS_CLIENT_SECRET=op://Vault/Modelfiche Remote Access/password
```

When that file exists, `mfiche start` automatically re-executes through
`op run`. Agents and operators use the same command:

```bash
mfiche start
```

Route `/api/*` to `127.0.0.1:8400` and the UI to `127.0.0.1:5173`. The remote
UI exchanges the credential once for a secure HTTP-only session. `mfiche`
automatically adds the configured credential and exact Origin when its
`--api-url` matches `TITLES_REMOTE_ACCESS_ORIGIN`:

```bash
op run --env-file=.env.1password -- \
  mfiche --api-url https://modelfiche.example.com --json doctor
```

Other automation may send `CF-Access-Client-Id` and
`CF-Access-Client-Secret` on each request; state-changing requests must also
send the exact configured `Origin`. See [SECURITY.md](SECURITY.md) before
enabling the tunnel.

## First-run checklist

1. Open **Settings** and configure the local profile and provider defaults.
2. Connect and verify an S3-compatible source when remote assets or training
   handoff are required.
3. Configure the FAL credential only when generation is required.
4. Run `mfiche backup create` and verify the resulting archive.
5. Run `mfiche --json doctor` and `mfiche --json training doctor`.

The detailed readiness checks remain available at `#/readiness`, but are no
longer a primary navigation destination.

## Common CLI workflows

Discover the installed contract rather than relying on remembered flags:

```bash
mfiche --help
mfiche --json capabilities
mfiche --json enums
mfiche <group> --help
mfiche <group> <command> --help
```

Inspect local state:

```bash
mfiche --json doctor
mfiche --json project list
mfiche --json job list
mfiche --json gallery list --limit 20
```

Create and verify a backup:

```bash
mfiche --json backup create
mfiche --json backup list
mfiche --json backup verify "/path/to/modelfiche-backup.tar.gz"
```

Prepare a trainer packet in the owning workspace:

```bash
mfiche --json --workspace <workspace-id-or-slug> training setup
mfiche --json --workspace <workspace-id-or-slug> training doctor
mfiche --json --workspace <workspace-id-or-slug> training run prepare <launch-id> \
  --output ./modelfiche-training-packet
```

`training setup` creates or reconciles the single W&B signing key used by all
Modelfiche workspaces and projects. Training launches select it automatically;
do not register a separate key for each project.

New launches require configured W&B ingress for live loss graphs and samples.
The guided CLI's explicit `--offline` option is archival-only; S3 checkpoint
backup is not telemetry ingestion. Export `trainer.env` before starting the
trainer and verify `run metrics` and `run samples`, not just launch creation.

See [docs/CLI.md](docs/CLI.md) for complete workflow examples and
[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) for the non-interactive contract.

## Storage and credentials

Import sources are standard S3 connections. For AWS, omit the endpoint and use
the boto3 credential chain. For S3-compatible services, configure the endpoint,
region, and `auto`, `path`, or `virtual` addressing.

Credentials are never stored in SQLite. A source stores only a credential
environment prefix. For a prefix such as `TRAINING`, the runtime reads:

```text
TRAINING_ACCESS_KEY
TRAINING_SECRET_KEY
TRAINING_SESSION_TOKEN  # optional
```

The default `S3` prefix also supports the standard AWS environment and boto3
credential chain. Provider secrets and signing private keys must remain outside
the repository and database.

## Backup and restore

Packaged upgrades create a verified pre-migration backup and abort before a
migration if backup verification fails.

```bash
mfiche backup create
mfiche backup list
mfiche backup verify "/path/to/backup.tar.gz"
```

When verified originals live in S3, back up only the database instead of
repacking remote assets:

```bash
mfiche --json backup create --database-only
mfiche backup restore "/path/to/database-backup.tar.gz" \
  --database-only \
  --confirm "RESTORE MODELFICHE BACKUP"
```

Single-copy maintenance is offline and dry-run by default. It publishes only
assets without an existing exact verified S3 location, under the selected
source's managed prefix; eviction removes local bytes only after an active
source has matching size and SHA-256 evidence:

```bash
mfiche --workspace "$WORKSPACE" --json storage single-copy audit --source "$SOURCE"
mfiche --workspace "$WORKSPACE" --json storage single-copy ensure-remote --source "$SOURCE"
mfiche stop
mfiche --workspace "$WORKSPACE" --json storage single-copy ensure-remote --source "$SOURCE" --apply
mfiche --workspace "$WORKSPACE" --json storage single-copy evict-local --apply
```

Restore is intentionally explicit and requires Modelfiche to be stopped:

```bash
mfiche stop
mfiche backup restore "/path/to/backup.tar.gz" \
  --confirm "RESTORE MODELFICHE BACKUP"
```

Use `mfiche backup recover` to roll back an interrupted directory swap.

## Development verification

```bash
uv run pytest -q
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
```

Build reproducible unsigned macOS release artifacts:

```bash
scripts/build_macos_app.sh
```

The ZIP, DMG, and SHA-256 manifest are written to `dist/macos/release/`.

## Architecture at a glance

```text
Web UI / mfiche CLI
        |
        v
Local FastAPI service ---- optional W&B-compatible ingress
        |
        +---- SQLite / PostgreSQL
        +---- local asset and derivative stores
        +---- S3-compatible origins and training handoff
        +---- durable worker ---- FAL / imports / exports / hydration
```

The API owns validation, transactions, activity records, provider admission,
and safety checks. The CLI and UI do not write directly to the database or
asset directories.

## Project status

Modelfiche is under active development. Compatibility-sensitive surfaces are
covered by migrations, API/CLI contract tests, pinned W&B protocol tests, and
frontend behavior tests. Review [SECURITY.md](SECURITY.md) before exposing any
service beyond the documented local boundary.
