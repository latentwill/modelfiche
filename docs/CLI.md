# `mfiche` CLI guide

`mfiche` is the supported command-line interface for operators, scripts, and
agents. It calls the same local API as the web UI; validation, activity records,
provider admission, and durable jobs remain server-side.

## Install and connect

The macOS application embeds the command at:

```bash
"/Applications/Modelfiche.app/Contents/MacOS/mfiche"
```

The Settings screen can install a user-scoped command at
`~/.local/bin/mfiche`. For development:

```bash
uv tool install --editable .
```

Without an installed entry point, use `uv run mfiche ...` from the repository.
The default API is `http://127.0.0.1:8400`; override it with `--api-url` or
`TITLES_API_URL`.

```bash
mfiche --api-url http://127.0.0.1:8400 --json doctor
```

For an authenticated loopback-tunnel deployment, resolve the same three values
used by the server. When the selected API URL exactly matches the configured
remote origin, the CLI sends the service credential and exact mutation Origin:

```bash
TITLES_REMOTE_ACCESS_ORIGIN=https://modelfiche.example.com
TITLES_REMOTE_ACCESS_CLIENT_ID=op://Vault/Modelfiche Remote Access/client id
TITLES_REMOTE_ACCESS_CLIENT_SECRET=op://Vault/Modelfiche Remote Access/password
op run --env-file=.env.1password -- \
  mfiche --api-url https://modelfiche.example.com --json doctor
```

Incomplete remote credentials fail locally before any request. The credential
has full single-operator authority; see [SECURITY.md](../SECURITY.md).

Global options must appear before the command group:

```text
--api-url <url>
--profile <id-or-name>
--workspace <id-or-slug>
--request-id <value>
--timeout <seconds>
--json
--jsonl
--quiet
```

## Discover the installed contract

The executable is canonical for its version:

```bash
mfiche --help
mfiche --json capabilities
mfiche --json enums
mfiche <group> --help
mfiche <group> <command> --help
```

Current top-level groups:

```text
context          workspace       profile          project
wandb            training        training-launch  training-launches
s3               transfer        import           job
dataset          caption         run              checkpoint
model            prompt-set      image            eval
grid             gallery         asset            review
comment          note            activity         lineage
connection       settings        backup           storage
server
```

Root shortcuts `mfiche start`, `mfiche status`, and `mfiche stop` delegate to
the matching `mfiche server` commands. When `.env.1password` exists, `start`
automatically resolves it with `op run` before it launches the stack.

## Output contract

Use `--json` for one JSON document and `--jsonl` for item-oriented output where
supported. With `--json`, stdout contains data only; diagnostics and wait
progress go to stderr. `--quiet` suppresses progress without changing results.

Errors use a stable envelope:

```json
{
  "ok": false,
  "error": {
    "code": "HTTP_409",
    "message": "The operation cannot run in the current state.",
    "retryable": false,
    "details": null
  }
}
```

Exit categories:

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `2` | Invalid command or arguments |
| `3` | Local context or request-boundary failure |
| `4` | Not found |
| `5` | Conflict or failed precondition |
| `6` | Provider, network, or timeout failure |
| `7` | Durable operation finished unsuccessfully |
| `8` | Local service unavailable |
| `9` | Partial success |

Do not infer retryability from the numeric exit code alone; inspect
`error.retryable` and the current resource state.

Structured API `detail` objects retain their domain `code`, `message`, and
`retryable` fields. A missing workspace (`workspace_required`, HTTP 428) exits
with code `3`; a cross-workspace entity mutation exits `5` with
`workspace_entity_mismatch` rather than losing the explanation to `HTTP_409`.

## Context and attribution

Profiles identify who performed an operation but do not grant access. For
unattended work, pass profile and workspace explicitly:

```bash
mfiche --json --profile agent-operator --workspace local project list
```

Inspect or set interactive defaults:

```bash
mfiche --json context show
mfiche --json workspace list
mfiche --json profile list
mfiche profile use <profile-id-or-name>
```

Use a caller-generated `--request-id` on supported mutations. Reuse the same
request ID only when retrying the same logical request.

Workspace references are IDs or slugs from `workspace list`, not display names.
The browser and CLI have independent selections. Unscoped reads use the oldest
workspace, so scope inspection as well as mutations. UI training handoff commands
include their workspace explicitly.

## Service lifecycle and diagnostics

```bash
mfiche start
mfiche status
mfiche --json doctor
mfiche stop
```

`start` waits for API and frontend readiness, then supervises the API, worker,
and web process as one stack. It does not silently detach a partially healthy
stack.

Inspect nonsecret operator settings and run safe configuration checks:

```bash
mfiche --json settings show
mfiche --json settings test storage
mfiche --json settings test fal
mfiche --json settings test llm
```

Use each command's `--help` before updating settings; accepted fields are
versioned with the installed CLI.
## S3 import workflow

Credentials remain in the environment. First create and verify a source:

```bash
mfiche s3 source-create \
  --name training-runs \
  --bucket my-training-bucket \
  --region us-east-1 \
  --allowed-prefix completed-runs/ \
  --credential-env-prefix TRAINING

mfiche --json s3 sources
mfiche --json s3 source-test <source-id>
```

Browse and preview before creating an import:

```bash
mfiche --json s3 ls \
  --source <source-id> \
  --prefix completed-runs/

mfiche --json import preview \
  --source <source-id> \
  --project <project-id> \
  --prefix completed-runs/example/
```

Create the durable import only after reviewing the preview:

```bash
mfiche --json --request-id import-example-001 import create \
  --source <source-id> \
  --project <project-id> \
  --prefix completed-runs/example/ \
  --wait
```

Inspect or recover it:

```bash
mfiche --json import show <import-id>
mfiche --json import retry <import-id>
mfiche --json import cancel <import-id>
```

## Dataset workflow

```bash
mfiche --json dataset list --project <project-id>
mfiche --json dataset show <dataset-id>
mfiche --json dataset versions <dataset-id>
mfiche --json dataset items <dataset-version-id>
```

Publishing creates an immutable version; it does not overwrite an earlier
version. Use `mfiche dataset publish --help` to review the confirmation and
input contract for the installed version.

## Jobs and recovery

Long operations return durable job or queue identities. Persist the ID before
waiting:

```bash
mfiche --json job show <job-id>
mfiche --json --quiet job wait <job-id> --timeout 3600
mfiche --json job logs <job-id>
```

Cancellation is cooperative and may be unavailable after a terminal state or a
provider dispatch. Retry only when the returned resource marks the operation as
retryable.

## Image, Eval, and Grid generation

Generation may be billable. Inspect endpoint schemas and preview before
submission:

```bash
mfiche --json image generate --help
mfiche --json eval endpoints
mfiche --json eval schema <endpoint-id>
mfiche --json eval preview --help
mfiche --json grid create --help
```

A configuration test or preview does not submit provider work. Use a new,
caller-owned request ID for an approved submission and persist the returned
queue/admission IDs.

Observe results:

```bash
mfiche --json eval show <eval-id>
mfiche --json eval outputs <eval-id>
mfiche --json grid show <grid-id>
mfiche --json grid cells <grid-id>
mfiche --json gallery list --project <project-id> --limit 50
```

## Training workflow

Checkpoints use outbound object storage. New launches require configured W&B
ingress for live losses and samples. Set `TITLES_WANDB_INGRESS_BASE_URL` to an
origin reachable from the trainer; follow the ingress routing guide.
Use `training run create --offline` only when intentionally choosing backup
without live graphs or samples.

`training setup` creates or reconciles the one W&B signing key shared by every
Modelfiche workspace and project. Launch creation selects it automatically.

```bash
mfiche --json --workspace <workspace-id-or-slug> training setup
mfiche --json --workspace <workspace-id-or-slug> training doctor
mfiche --json --workspace <workspace-id-or-slug> training run create \
  --dataset <dataset-id-or-name> \
  --name portrait-lora-v4 \
  --trainer ai-toolkit \
  --base-model FLUX.1-dev
mfiche --json --workspace <workspace-id-or-slug> training run preflight <launch-id>
mfiche --json --workspace <workspace-id-or-slug> training run prepare <launch-id> \
  --output ./modelfiche-training-packet
```

The packet includes `training-dataset.zip`, `manifest.json`, `trainer.env`,
trainer-specific logging metadata, `modelfiche-run-sync.py`, and
`checksums.json`. Copy it over an encrypted channel, verify checksums, install
`wandb==0.28.0` for live logging, supply the named S3 credentials, and export
the environment with `set -a; . ./trainer.env; set +a`. Run the sync helper beside
the selected trainer. For Krea 2 embeddings, pass `--trainer kef-krea2`; optimization,
embedding, memory, and flow-matching settings are explicit launch options:

```bash
mfiche --json --workspace <workspace-id-or-slug> training run create \
  --dataset <dataset-id-or-name> \
  --name lotus-rocks-kef-v002 \
  --trainer kef-krea2 \
  --base-model krea/Krea-2-Raw \
  --steps 8000 \
  --tokens 5 \
  --learning-rate 5e-4 \
  --batch-size 1 \
  --gradient-accumulation 4 \
  --resolution 512 \
  --sample-interval 250 \
  --caption-mode random \
  --model-revision <RAW_MODEL_COMMIT>
```

`--caption-mode none` starts Krea training with empty text conditioning. The
launch still exports the dataset and emits the same DAM and W&B telemetry.

The Krea packet contains `kef-krea2-telemetry.json` and
`kef-krea2-training.json`. The latter preserves the validated configuration
and exact trainer argument vector; source `trainer.env` before starting it.

Observe the run:

```bash
mfiche --json --workspace <workspace-id-or-slug> run live <run-id>
mfiche --json --workspace <workspace-id-or-slug> run metrics <run-id>
mfiche --json --workspace <workspace-id-or-slug> run samples <run-id> --limit 20
mfiche --json --workspace <workspace-id-or-slug> run checkpoints <run-id>
mfiche --json --workspace <workspace-id-or-slug> run lineage <run-id>
```

Inspect available metric names before filtering: AI Toolkit and kef-krea2 do
not necessarily use the same loss key. See
[WANDB_COMPATIBILITY.md](WANDB_COMPATIBILITY.md) for live telemetry and protocol details.

## Reviews, comments, and lineage

Commands that accept a subject use a typed reference such as
`asset:<uuid>`, `checkpoint:<uuid>`, `run:<uuid>`, or `eval_output:<uuid>`.
Confirm the accepted subject names with command help.

```bash
mfiche --json review show asset:<asset-id>
mfiche --json review set asset:<asset-id> --rating 5 --decision approved
mfiche --json comment list asset:<asset-id>
mfiche --json lineage show checkpoint:<checkpoint-id>
```

Review decisions can affect production selection, archive eligibility, or
other downstream actions. Read the command's consequence text before mutation.

## Backup, restore, and support

```bash
mfiche --json backup create
mfiche --json backup list
mfiche --json backup verify "/path/to/backup.tar.gz"
```

Stop the application before restore:

```bash
mfiche stop
mfiche backup restore "/path/to/backup.tar.gz" \
  --confirm "RESTORE MODELFICHE BACKUP"
```

Use `mfiche backup recover` after an interrupted directory swap. The Settings
Diagnostics section can download a redacted support bundle; review it before
sharing.

## Shell completion

Typer provides completion scripts for supported shells:

```bash
mfiche --show-completion
mfiche --install-completion
```

For automation rules and copy-ready control flow, continue with
[AGENT_GUIDE.md](AGENT_GUIDE.md).
