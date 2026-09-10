# Modelfiche CLI contract

## 1. Purpose

The `mfiche` CLI is the supported automation surface for operators, scripts,
and coding agents. It exposes the same meaningful operations as the web
application without duplicating business logic or requiring interactive
terminal input.

This file defines stable behavior and safety requirements. The exact command
tree for an installed version is `mfiche --help`; current workflows are
documented in [docs/CLI.md](docs/CLI.md), and unattended usage is documented in
[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md).

## 2. Architecture

The CLI calls the local Modelfiche DAM HTTP API. Domain validation, transactions, activity events, provider access, and safety checks remain in the application service layer.

```text
Agent or shell
  -> mfiche CLI
  -> localhost API
  -> application services
  -> SQLite / worker / local assets / MEGA S4 / FAL
```

The CLI must never write directly to SQLite or the asset directory. This keeps UI, CLI, and future authenticated web behavior consistent.

The Windows package exposes the same command tree as `mfiche.exe`. Its daemon
commands manage the packaged launcher, API, worker, and W&B ingress; the built
web UI is served by the API. `status` requires every service to be healthy and
returns exit code 8 when the stack is stopped or unhealthy. `stop` waits for
service shutdown. The source checkout continues to run the development frontend.

Required daemon commands:

```text
mfiche server start
mfiche server status
mfiche server stop
mfiche doctor
```

`server start` runs or supervises the local API, worker, and frontend as one
stack. If any service exits or fails its startup check, the supervisor stops
the remaining services. Other commands fail clearly when the service is
unavailable; they do not silently start background processes.

## 3. Agent Contract

Every data command supports:

```text
--json                 one JSON result document
--jsonl                one JSON object per streamed/list item
--quiet                suppress human progress output
--profile <id|name>    attribute the operation to a local profile
--workspace <id|slug>  explicit workspace context
--request-id <value>   caller-supplied idempotency/correlation key
--timeout <seconds>    client timeout, not job cancellation
```

Mutating commands additionally support, where relevant:

```text
--dry-run
--wait
--no-wait
--if-version <value>
--yes
```

Rules:

- Non-interactive execution is always possible.
- `--json` writes only JSON to stdout. Progress and diagnostics go to stderr.
- Timestamps use ISO 8601 UTC.
- IDs, enums, and field names are stable and documented.
- Lists use cursor pagination, never unbounded output by default.
- Long operations return a durable job object unless `--wait` is supplied.
- `--request-id` makes supported mutations idempotent.
- Errors include a stable code, human message, retryability, and structured details.
- Secrets and presigned URLs are redacted unless a command explicitly exists to request a short-lived access URL.

Example error:

```json
{
  "ok": false,
  "error": {
    "code": "IMPORT_PREFIX_NOT_DETECTED",
    "message": "The selected prefix is not a recognized dataset or training run.",
    "retryable": false,
    "details": {
      "prefix": "title-lora/projects/title/misc/",
      "signals": []
    }
  }
}
```

Exit codes:

```text
0  success
2  invalid command or arguments
3  authentication/profile/workspace context error
4  not found
5  conflict or failed precondition
6  provider/network error
7  asynchronous job failed
8  local service unavailable
9  partial success
```

Exit code `3` initially represents missing or invalid local profile context; it does not imply login authentication.
Missing workspace context (HTTP 428) exits `3`. Nested API `detail` objects
preserve their domain error code, message, retryability, and structured details.

## 4. Context and Profiles

Profiles provide attribution without authentication.

```text
mfiche profile list
mfiche profile create --name "Ed Kennedy" --email "..."
mfiche profile show <profile>
mfiche profile use <profile>
mfiche profile update <profile> --name "..."
mfiche profile archive <profile>
mfiche context show
```

The active profile is a workstation preference. Agents should pass `--profile` explicitly for unattended operations so attribution does not depend on mutable local state.

Comments, notes, ratings, decisions, imports, dataset publications, model-version creation, and configuration changes record the selected profile ID.

## 5. Command Tree

### System and configuration

```text
mfiche version
mfiche capabilities
mfiche enums
mfiche doctor
mfiche start|status|stop
mfiche server start|status|stop
mfiche context show|set
mfiche connection list|test
mfiche settings show|update|test
```

Secret values are environment/1Password supplied and cannot be printed by `config show`.

### Projects

```text
mfiche project list [--state active]
mfiche project create --name <name> [--trigger <word> ...]
mfiche project show <project>
mfiche project update <project> [fields]
mfiche project archive <project>
mfiche project activity <project>
```

### S3 browsing and detection

```text
mfiche s3 sources
mfiche s3 ls [<prefix>] [--cursor <cursor>] [--limit 100]
mfiche s3 stat <key>
mfiche s3 detect <prefix>
mfiche s3 tree <prefix> --depth 2 --limit 500
```

The CLI enforces the same allowed roots as the UI. It does not expose delete, move, rename, upload, ACL, or arbitrary bucket commands in the operator releases.

Example:

```bash
mfiche s3 detect \
  title-lora/projects/title/runs/kzapata-specialist-track-b-v002/ \
  --json
```

Expected result includes declared versus observed counts, detected trainer, dataset references, history presence, estimated import/cache work, and warnings.

### Imports and jobs

```text
mfiche import create --project <project> --prefix <prefix> [--kind dataset|run]
mfiche import preview --project <project> --prefix <prefix>
mfiche import show <import-job>
mfiche import retry <import-job>
mfiche import cancel <import-job>
mfiche import refresh <imported-object>

mfiche job list [--state running]
mfiche job show <job>
mfiche job wait <job> [--timeout 600]
mfiche job cancel <job>
mfiche job logs <job> [--tail 100]
```

`import preview` is read-only. `import create --dry-run` returns the same detection and planned object policy without creating records.

### Datasets

```text
mfiche dataset list [--project <project>]
mfiche dataset show <dataset>
mfiche dataset versions <dataset>
mfiche dataset items <dataset-version> [filters]
mfiche dataset draft create <dataset-version>
mfiche dataset draft show <draft>
mfiche dataset draft discard <draft>
mfiche dataset publish <draft> --name <version-name>
```

Caption operations:

```text
mfiche caption preview <draft> replace --find <text> --replace <text> [scope]
mfiche caption apply   <draft> replace --find <text> --replace <text> [scope]
mfiche caption preview <draft> add-word --word <word> [scope]
mfiche caption apply   <draft> add-word --word <word> [scope]
mfiche caption set <draft> <item> --text <caption>
mfiche caption generate <draft> --prompt <prompt> [--model <model>] [--all|--item <id> ...]
mfiche caption import <draft> --file <jsonl-or-directory>
mfiche caption export <dataset-version> --format jsonl|txt-pairs --output <path>
```

Scopes use explicit filters rather than UI selection state:

```text
--item <id> ...
--where-tag <tag>
--where-included true|false
--where-caption-format text|json
--all
```

Batch mutation without an explicit scope is rejected.

### Global W&B signing key and training launches

```text
mfiche training setup [--private-key-file <path>]
mfiche training doctor

mfiche wandb key generate --private-key-output <path>
mfiche wandb key public --private-key-file <path>
mfiche wandb credential register --public-key <key>
mfiche wandb credential list
mfiche wandb credential rotate --public-key <key>
mfiche wandb credential revoke

mfiche training-launch create --input <launch.json>
mfiche training-launch show <launch>
mfiche training-launch manifest <launch> [--output <path>]
mfiche training-launch environment <launch> [--output <path>]
mfiche training-launch agent-environment <launch> --private-key-file <path> [--output <path>]
mfiche training-launch inspect <launch>
mfiche training-launch wait <launch> [--timeout <seconds>] [--poll-interval <seconds>]
```

Modelfiche has one W&B signing key for all workspaces and projects.
`training setup` creates or reads the local private key, then registers or
rotates the one global public key. Launch creation selects that key
automatically; launch input has no credential ID or scope.

Private key generation is local. `credential register` sends only the public
key to the API. `agent-environment` signs the exact launch claims locally; with
`--output`, it writes the secret-bearing shell environment with mode `0600`.
Agents should avoid emitting that environment into command transcripts.

`training-launch inspect` returns `{launch, run}` so an agent can evaluate
launch state, live metrics, upload counts, and handoff state without joining
multiple responses. `training-launch wait` exits `0` only for completion and
exits `7` for failed or interrupted launches.

### Runs, checkpoints, samples, and models

```text
mfiche run list [--project <project>] [--status <status>]
mfiche run show <run>
mfiche run live <run>
mfiche run metrics <run> [--name <metric>]
mfiche run refresh <run>
mfiche run config <run> [--raw|--normalized]
mfiche run checkpoints <run>
mfiche run samples <run> [--step <step>]
mfiche run lineage <run>

mfiche checkpoint show <checkpoint>
mfiche checkpoint download <checkpoint> [--output <path>]
mfiche checkpoint verify <checkpoint>

mfiche model list [--project <project>]
mfiche model create --project <project> --name <name>
mfiche model version create <model> --checkpoint <checkpoint> --state candidate
mfiche model version approve <model-version>
mfiche model version archive <model-version>
mfiche model version show <model-version>
```

Creating a model version records intentional use of a checkpoint. It does not alter, copy, or move the source checkpoint unless an explicit download is requested.

### Prompt sets and Evals

```text
mfiche prompt-set list [--project <project>]
mfiche prompt-set create --project <project> --name <name>
mfiche prompt-set import --project <project> --file <json>
mfiche prompt-set export <prompt-set> --output <file>
mfiche prompt-set version <prompt-set> --file <json>

mfiche eval endpoints
mfiche eval schema <endpoint>
mfiche eval preview --model-version <id> --prompt-set <id> --endpoint <id> [params]
mfiche eval create --model-version <id> --prompt-set <id> --endpoint <id> [params]
mfiche eval show <eval-run>
mfiche eval wait <eval-run>
mfiche eval cancel <eval-run>
mfiche eval outputs <eval-run>
mfiche eval lineage <eval-run>
```

Endpoint-specific arguments may be supplied with repeatable structured parameters:

```text
--param steps=28
--param strength=0.85
```

`eval schema --json` is the authoritative discovery mechanism for agents.

### Grids

```text
mfiche grid create --model-version <id> --prompt-set <id> --endpoint <id> \
  --x-axis steps --x-values 18,24,32,40 \
  --y-axis strength --y-values 0.6,0.8,1.0
mfiche grid show <grid>
mfiche grid run <grid>
mfiche grid wait <grid-run>
mfiche grid cells <grid-run>
mfiche grid export-metadata <grid-run> --output <json>
```

The CLI does not generate a giant composite by default. An optional contact-sheet export can be added later as a derived artifact.

### Gallery and assets

```text
mfiche gallery list [filters]
mfiche asset show <asset>
mfiche asset locations <asset>
mfiche asset metadata <asset>
mfiche asset thumbnail <asset> --size 512 --output <path>
mfiche asset download <asset> --output <path>
mfiche asset lineage <asset> [--direction both] [--depth 3]
```

Gallery filters include project, dataset, run, model version, Eval, asset kind, rating, decision, prompt, seed, step, and hydration state.

### Reviews, comments, and notes

```text
mfiche review set <subject> [--rating 1..5] [--decision candidate|approved|hold|reject]
mfiche review show <subject>
mfiche review list [filters]

mfiche comment list <subject>
mfiche comment add <subject> --text <text>
mfiche comment update <comment> --text <text>

mfiche note list <subject>
mfiche note add <subject> --file <markdown-file>
mfiche note show <note>
```

Subjects use typed references:

```text
asset:<uuid>
checkpoint:<uuid>
model-version:<uuid>
eval-output:<uuid>
prompt-set:<uuid>
dataset-version:<uuid>
```

### Activity and lineage

```text
mfiche activity list [--project <project>] [--profile <profile>] [--since <time>]
mfiche lineage show <typed-subject> [--depth 3]
mfiche lineage path <typed-subject-a> <typed-subject-b>
```

## 6. Safety Model

Read operations require no confirmation.

Mutations are divided into:

- **Reversible:** comments, draft edits, ratings, and candidate/hold decisions.
- **Version-creating:** dataset publish, prompt-set version, model-version creation.
- **Provider-executing:** FAL Eval or Grid generation.
- **Artifact-transfer:** checkpoint or original-asset download.

Rules:

- Provider-executing workflows expose a preview, schema, preflight, or explicit
  consequence review before submission.
- Destructive and restore commands require the confirmation contract shown by
  command help; agents must not synthesize human confirmation.
- No S3 object-delete command exists in the operator CLI.
- Dataset publication creates a new immutable version.
- Model-version approval does not modify checkpoint bytes.
- Downloads and restore operations verify content before atomic replacement.
- Mutable resources use explicit version/state preconditions where supported.

## 7. Agent Discoverability

The CLI must be self-describing without prose scraping:

```text
mfiche --json capabilities
mfiche --json enums
mfiche --json eval endpoints
mfiche --json eval schema <endpoint>
mfiche <group> --help
mfiche <group> <command> --help
```

`mfiche capabilities` returns supported server features, not configuration or
end-to-end readiness. Its `agent_contract` identifies workspace/profile headers,
the explicit workspace mutation requirement, unscoped-read behavior, OpenAPI,
and training/asset inspection paths. Use `training doctor` and per-launch
preflight for readiness. New training launches require configured live ingress;
`training run create --offline` explicitly selects archival-only operation.
`mfiche enums` returns the installed API's accepted enum values.
Endpoint-specific generation schemas come from the admission adapter registry.

Offline storage maintenance is explicit and dry-run first:

```text
mfiche --workspace <workspace> --json storage single-copy audit --source <source>
mfiche --workspace <workspace> --json storage single-copy ensure-remote --source <source>
mfiche --workspace <workspace> --json storage single-copy ensure-remote --source <source> --apply
mfiche --workspace <workspace> --json storage single-copy evict-local --apply
mfiche --json backup create --database-only
```

Applied storage mutations require Modelfiche to be stopped. `ensure-remote`
skips any existing exact verified S3 location and uses deterministic managed
keys. `evict-local` requires an active remote source with matching size and
SHA-256 evidence. Database-only archives contain recovery state, not asset
bytes, and restore only through the explicit database-only restore path.

The maintained documentation surfaces are:

- `docs/CLI.md` for operator installation and workflows;
- `docs/AGENT_GUIDE.md` for deterministic automation and safety rules;
- `docs/WANDB_COMPATIBILITY.md` for AI Toolkit and trainer transport;
- OpenAPI for the canonical local network schema.

## 8. Compatibility and Versioning

- `mfiche --json version` reports the CLI version and available server metadata.
- The CLI refuses unsafe major-version mismatches with the local API.
- Additive JSON fields are allowed in minor releases.
- Existing fields and enum meanings do not change silently.
- Deprecated commands emit structured warnings to stderr and remain available for at least one minor release.
- Job results retain the command schema version used to create them.

## 9. Release Integration

### Release 0

- CLI skeleton, server control, doctor, capability discovery, profiles, and job inspection.
- JSON/error/exit-code contract tests.

### Release 1

- Full project, S3 browse/detect, import, dataset, caption, run, checkpoint inventory, Gallery, asset, and comment commands.
- End-to-end parity test: import and publish the same dataset once through API fixtures and once through CLI, then compare domain results.

### Release 2

- Model-version, sample comparison data, prompt-set, FAL Eval, review, decision, activity, and lineage commands.

### Release 3

- Grid commands, saved filters, performance diagnostics, and complete generated reference documentation.

### Release 4

- CLI authentication token/profile support for remote deployments while preserving the local no-auth mode.
- Local profile IDs map to authenticated account identities during migration.

## 10. Acceptance Criteria

The CLI is complete when:

1. Every meaningful UI mutation has a corresponding CLI command or is explicitly documented as presentation-only.
2. Agents can discover commands, inputs, enum values, and provider schemas through JSON.
3. Every command works without prompts when all required arguments are supplied.
4. Imports and provider jobs are resumable and inspectable after process restarts.
5. Repeated commands with the same request ID do not duplicate mutations.
6. CLI and UI operations share validation, authorization/profile attribution, transactions, and activity events.
7. No command exposes stored credentials or unrestricted remote-storage mutation.
8. Golden-path CLI tests cover S3 import, dataset publication, run inspection, model-version creation, FAL Eval generation, review, and lineage lookup.
