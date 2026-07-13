# Titles DAM Agentic CLI Specification

## 1. Purpose

The CLI is a complete automation surface for operators, scripts, and coding agents. It must support the same meaningful operations as the web application without duplicating business logic or requiring interactive terminal input.

Working command name:

```text
titles
```

The final binary name can change before implementation. Command and JSON contracts should remain stable once Release 1 is operational.

## 2. Architecture

The CLI calls the local Titles DAM HTTP API. Domain validation, transactions, activity events, provider access, and safety checks remain in the application service layer.

```text
Agent or shell
  -> titles CLI
  -> localhost API
  -> application services
  -> SQLite / worker / local assets / MEGA S4 / FAL
```

The CLI must never write directly to SQLite or the asset directory. This keeps UI, CLI, and future authenticated web behavior consistent.

Required daemon commands:

```text
titles server start
titles server status
titles server stop
titles doctor
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
--workspace <id|name>  explicit workspace context
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
3  authentication/profile context error
4  not found
5  conflict or failed precondition
6  provider/network error
7  asynchronous job failed
8  local service unavailable
9  partial success
```

Exit code `3` initially represents missing or invalid local profile context; it does not imply login authentication.

## 4. Context and Profiles

Profiles provide attribution without authentication.

```text
titles profile list
titles profile create --name "Ed Kennedy" --email "..."
titles profile show <profile>
titles profile use <profile>
titles profile update <profile> --name "..."
titles profile archive <profile>
titles context show
```

The active profile is a workstation preference. Agents should pass `--profile` explicitly for unattended operations so attribution does not depend on mutable local state.

Comments, notes, ratings, decisions, imports, dataset publications, model-version creation, and configuration changes record the selected profile ID.

## 5. Command Tree

### System and configuration

```text
titles version
titles doctor [--json]
titles server start|status|stop
titles config show
titles config set <key> <value>
titles connection list
titles connection test mega
titles connection test fal
```

Secret values are environment/1Password supplied and cannot be printed by `config show`.

### Projects

```text
titles project list [--state active]
titles project create --name <name> [--trigger <word> ...]
titles project show <project>
titles project update <project> [fields]
titles project archive <project>
titles project activity <project>
```

### S3 browsing and detection

```text
titles s3 sources
titles s3 ls [<prefix>] [--cursor <cursor>] [--limit 100]
titles s3 stat <key>
titles s3 detect <prefix>
titles s3 tree <prefix> --depth 2 --limit 500
```

The CLI enforces the same allowed roots as the UI. It does not expose delete, move, rename, upload, ACL, or arbitrary bucket commands in the operator releases.

Example:

```bash
titles s3 detect \
  title-lora/projects/title/runs/kzapata-specialist-track-b-v002/ \
  --json
```

Expected result includes declared versus observed counts, detected trainer, dataset references, history presence, estimated import/cache work, and warnings.

### Imports and jobs

```text
titles import create --project <project> --prefix <prefix> [--kind dataset|run]
titles import preview --project <project> --prefix <prefix>
titles import show <import-job>
titles import retry <import-job>
titles import cancel <import-job>
titles import refresh <imported-object>

titles job list [--state running]
titles job show <job>
titles job wait <job> [--timeout 600]
titles job cancel <job>
titles job logs <job> [--tail 100]
```

`import preview` is read-only. `import create --dry-run` returns the same detection and planned object policy without creating records.

### Datasets

```text
titles dataset list [--project <project>]
titles dataset show <dataset>
titles dataset versions <dataset>
titles dataset items <dataset-version> [filters]
titles dataset draft create <dataset-version>
titles dataset draft show <draft>
titles dataset draft discard <draft>
titles dataset publish <draft> --name <version-name>
```

Caption operations:

```text
titles caption preview <draft> replace --find <text> --replace <text> [scope]
titles caption apply   <draft> replace --find <text> --replace <text> [scope]
titles caption preview <draft> add-word --word <word> [scope]
titles caption apply   <draft> add-word --word <word> [scope]
titles caption set <draft> <item> --text <caption>
titles caption import <draft> --file <jsonl-or-directory>
titles caption export <dataset-version> --format jsonl|txt-pairs --output <path>
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

### W&B credentials and training launches

```text
titles wandb key generate --private-key-output <path>
titles wandb key public --private-key-file <path>
titles wandb credential register --alias <alias> --public-key <key> [--project <id>]
titles wandb credential list [--project <id>]
titles wandb credential rotate <credential> --public-key <key>
titles wandb credential revoke <credential>

titles training-launch create --input <launch.json>
titles training-launch show <launch>
titles training-launch manifest <launch> [--output <path>]
titles training-launch environment <launch> [--output <path>]
titles training-launch agent-environment <launch> --private-key-file <path> [--output <path>]
titles training-launch inspect <launch>
titles training-launch wait <launch> [--timeout <seconds>] [--poll-interval <seconds>]
```

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
titles run list [--project <project>] [--status <status>]
titles run show <run>
titles run live <run>
titles run metrics <run> [--name <metric>]
titles run refresh <run>
titles run config <run> [--raw|--normalized]
titles run checkpoints <run>
titles run samples <run> [--step <step>]
titles run lineage <run>

titles checkpoint show <checkpoint>
titles checkpoint download <checkpoint> [--output <path>]
titles checkpoint verify <checkpoint>

titles model list [--project <project>]
titles model create --project <project> --name <name>
titles model version create <model> --checkpoint <checkpoint> --state candidate
titles model version approve <model-version>
titles model version archive <model-version>
titles model version show <model-version>
```

Creating a model version records intentional use of a checkpoint. It does not alter, copy, or move the source checkpoint unless an explicit download is requested.

### Prompt sets and Evals

```text
titles prompt-set list [--project <project>]
titles prompt-set create --project <project> --name <name>
titles prompt-set import --project <project> --file <json>
titles prompt-set export <prompt-set> --output <file>
titles prompt-set version <prompt-set> --file <json>

titles eval endpoints
titles eval schema <endpoint>
titles eval preview --model-version <id> --prompt-set <id> --endpoint <id> [params]
titles eval create --model-version <id> --prompt-set <id> --endpoint <id> [params]
titles eval show <eval-run>
titles eval wait <eval-run>
titles eval cancel <eval-run>
titles eval outputs <eval-run>
titles eval lineage <eval-run>
```

Endpoint-specific arguments may be supplied with repeatable structured parameters:

```text
--param steps=28
--param strength=0.85
```

`eval schema --json` is the authoritative discovery mechanism for agents.

### Grids

```text
titles grid create --model-version <id> --prompt-set <id> --endpoint <id> \
  --x-axis steps --x-values 18,24,32,40 \
  --y-axis strength --y-values 0.6,0.8,1.0
titles grid show <grid>
titles grid run <grid>
titles grid wait <grid-run>
titles grid cells <grid-run>
titles grid export-metadata <grid-run> --output <json>
```

The CLI does not generate a giant composite by default. An optional contact-sheet export can be added later as a derived artifact.

### Gallery and assets

```text
titles gallery list [filters]
titles asset show <asset>
titles asset locations <asset>
titles asset metadata <asset>
titles asset thumbnail <asset> --size 512 --output <path>
titles asset download <asset> --output <path>
titles asset lineage <asset> [--direction both] [--depth 3]
```

Gallery filters include project, dataset, run, model version, Eval, asset kind, rating, decision, prompt, seed, step, and hydration state.

### Reviews, comments, and notes

```text
titles review set <subject> [--rating 1..5] [--decision candidate|approved|hold|reject]
titles review show <subject>
titles review list [filters]

titles comment list <subject>
titles comment add <subject> --text <text>
titles comment update <comment> --text <text>

titles note list <subject>
titles note add <subject> --file <markdown-file>
titles note show <note>
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
titles activity list [--project <project>] [--profile <profile>] [--since <time>]
titles lineage show <typed-subject> [--depth 3]
titles lineage path <typed-subject-a> <typed-subject-b>
```

## 6. Safety Model

Read operations require no confirmation.

Mutations are divided into:

- **Reversible:** comments, draft edits, ratings, and candidate/hold decisions.
- **Version-creating:** dataset publish, prompt-set version, model-version creation.
- **Provider-executing:** FAL Eval or Grid generation.
- **Artifact-transfer:** checkpoint or original-asset download.

Rules:

- All version-creating and provider-executing commands support `--dry-run`.
- Provider execution returns estimated image/job count before submission.
- `--yes` is required only when a human-oriented invocation would otherwise confirm. Agents can use an explicit `--request-id` plus `--yes`.
- No S3 delete/write commands exist in the operator CLI.
- Dataset publish never overwrites a previous version.
- Model-version approval does not modify the underlying checkpoint.
- Download commands check free space and use atomic temporary files.
- Optimistic concurrency uses `--if-version` for mutable records.

## 7. Agent Discoverability

The CLI must be self-describing without prose scraping:

```text
titles capabilities --json
titles schema command <command-path> --json
titles eval schema <endpoint> --json
titles enums --json
```

`titles capabilities` returns feature availability, API/schema versions, configured providers, supported endpoint adapters, allowed S3 roots, and maximum operation limits.

OpenAPI remains the canonical network contract. The CLI publishes JSON Schemas for command inputs and outputs generated from the same Pydantic models.

Provide:

- `docs/cli/` command reference generated from source.
- `docs/automation/recipes.md` with deterministic examples.
- A small Codex/agent skill that teaches capability discovery and safety rules without embedding environment credentials.

## 8. Compatibility and Versioning

- `titles version --json` reports CLI, API, database schema, and command schema versions.
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
