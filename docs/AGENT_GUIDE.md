# Agent automation guide

This guide is for coding agents, shell agents, and unattended scripts operating
Modelfiche through `mfiche`. The CLI is the supported automation boundary. Do
not read or mutate SQLite, local asset directories, runtime lock files, or
provider state directly.

## Drop-in operating instructions

Use this block as repository or task context for an automation agent:

```text
Operate Modelfiche only through the mfiche CLI or documented local API.
Start with `mfiche --json capabilities`, `mfiche --json doctor`, and the
relevant `<group> --help`. Put global options before the command group.
Use --json for one result document, --jsonl only where supported, and never
scrape human tables. Pass --profile, --workspace, and a caller-owned
--request-id for unattended mutations. Persist returned resource, job, queue,
and admission IDs before waiting. Preview provider work and surface its cost
consequence before submission. Never print secrets, signed tokens, presigned
URLs, trainer.env, or private keys. Retry only when the error or resource says
the operation is retryable; reuse a request ID only for the same logical
request. Verify terminal state and resulting resources before reporting success.
```

## Windows package

Use the extracted package's `mfiche.exe` for its matching command contract.
In PowerShell, invoke `.\mfiche.exe --json capabilities` and
`.\mfiche.exe --json workspace list`. The same global flags and explicit
workspace rules apply. `start`, `status`, and `stop` control the bundled services.
Keep the application folder intact; data lives in `%LOCALAPPDATA%\Modelfiche`.

## Bootstrap sequence

Resolve the command once, then inspect runtime capabilities:

```bash
MFICHE="${MFICHE:-mfiche}"

"$MFICHE" --json version
"$MFICHE" --json capabilities
"$MFICHE" --json enums
"$MFICHE" --json doctor
```

If `mfiche` is not installed in a development checkout, invoke
`uv run mfiche ...` directly. Do not set `MFICHE="uv run mfiche"` with the
quoted `"$MFICHE"` recipe above: shells would look for one executable whose
name contains spaces. Prefer the checkout command when developing; a globally
installed CLI can be older than the running API.

For unattended operations, add explicit context before the command group:

```bash
mfiche \
  --json \
  --quiet \
  --workspace "$WORKSPACE_ID" \
  --profile "$PROFILE_ID" \
  project list
```

Use workspace IDs or slugs from `workspace list`, not display names. A workspace
selected in the browser does not select the CLI workspace. Unscoped reads use
the oldest workspace; they are not an all-workspace search. Resolve profiles
and projects inside the selected workspace:

```bash
mfiche --json --workspace "$WORKSPACE_ID" profile list
mfiche --json --workspace "$WORKSPACE_ID" project list
mfiche --json --workspace "$WORKSPACE_ID" project show "$PROJECT_ID"
```

`capabilities.agent_contract` describes headers and inspection paths. Capability
support is not readiness: run domain checks before starting work.

## Control flow

Use this order for every mutation:

1. **Discover** — call `capabilities`, `enums`, and command help.
2. **Resolve context** — use explicit workspace/profile IDs.
3. **Read current state** — fetch the target and its version/status.
4. **Preview** — use the domain preview, schema, preflight, or safe test command.
5. **Explain consequence** — identify billing, version creation, transfer, or
   destructive effects.
6. **Mutate once** — provide a unique request ID when supported.
7. **Persist identity** — save returned IDs before polling.
8. **Observe** — poll the durable job/queue/resource, not human output.
9. **Verify effect** — read the resulting domain object and invariants.
10. **Report evidence** — include IDs, terminal state, and verified result.

A successful HTTP response that creates a job is not proof that the underlying
operation succeeded.

## JSON and stderr

With `--json`, parse exactly one stdout document. Progress and diagnostics are
written to stderr. Keep both streams separate:

```bash
mfiche --json --quiet job show "$JOB_ID" >job.json 2>job.stderr
```

Do not strip lines from stdout or search human labels. Treat additive JSON
fields as compatible. Branch on stable fields such as `status`, `state`,
`ready`, `retryable`, `id`, and documented error codes.

## Exit handling

```text
0  success
2  invalid arguments
3  local context or request-boundary failure
4  not found
5  conflict or failed precondition
6  provider, network, or timeout failure
7  durable operation failed or was interrupted
8  local service unavailable
9  partial success
```

Rules:

- Exit `6` is not automatically retryable.
- Exit `7` requires reading the terminal resource and its error.
- A timeout does not prove server-side cancellation.
- On conflict, re-read state before deciding whether the desired effect already
  happened.
- Never retry a provider submission with a new request ID until the previous
  admission/queue state is known.

## Idempotency

Generate request IDs outside the retry loop:

```bash
REQUEST_ID="agent-import-$(python -c 'import uuid; print(uuid.uuid4())')"

mfiche --json --request-id "$REQUEST_ID" import create \
  --source "$SOURCE_ID" \
  --project "$PROJECT_ID" \
  --prefix "$PREFIX" \
  --wait
```

Reuse `REQUEST_ID` only to retry that exact source, project, prefix, and intent.
A changed payload requires a new ID.

## Durable jobs

Capture the ID from the create response, then wait separately when practical:

```bash
mfiche --json job show "$JOB_ID"
mfiche --json --quiet job wait "$JOB_ID" --timeout 3600
mfiche --json job logs "$JOB_ID"
```

Do not assume every job supports cancellation. If cancellation is accepted,
continue polling until the resource reaches a terminal state.

## Safe provider workflow

FAL generation is potentially billable. LLM prompt generation may also incur
provider cost. A safe agent:

1. reads endpoint schemas and configured limits;
2. runs a preview or preflight;
3. calculates the request count from the returned plan;
4. presents the consequence to the caller;
5. submits only after the task contains clear authorization;
6. stores the admission, queue, and provider-job identities;
7. verifies generated assets and provenance after completion.

Configuration checks under `mfiche settings test ...` are non-billable and must
not be described as provider execution.

## Import recipe

```bash
mfiche --json s3 source-test "$SOURCE_ID"
mfiche --json s3 detect --source "$SOURCE_ID" --prefix "$PREFIX"
mfiche --json import preview \
  --source "$SOURCE_ID" \
  --project "$PROJECT_ID" \
  --prefix "$PREFIX"
mfiche --json --quiet --request-id "$REQUEST_ID" import create \
  --source "$SOURCE_ID" \
  --project "$PROJECT_ID" \
  --prefix "$PREFIX" \
  --wait
```

After completion, read the import, resulting dataset/run, and activity event.
Do not infer success solely from object counts in progress output.

## Training recipe

Create the launch in the workspace that owns the project and dataset. The global
signing key is shared; dataset versions, sources, and runs are not. Set all IDs
from scoped lookups rather than deriving them from names.

```bash
mfiche --json --workspace "$WORKSPACE_ID" training setup
mfiche --json --workspace "$WORKSPACE_ID" training doctor
mfiche --json --workspace "$WORKSPACE_ID" dataset list --project "$PROJECT_ID"
mfiche --json --workspace "$WORKSPACE_ID" --request-id "$REQUEST_ID" \
  training run create --dataset "$DATASET_ID" --name "$RUN_NAME" \
  --base-model "$BASE_MODEL" --source "$SOURCE_ID"
# Persist the returned launch.id and launch.run_id before continuing.
mfiche --json --workspace "$WORKSPACE_ID" training run preflight "$LAUNCH_ID"
mfiche --json --workspace "$WORKSPACE_ID" training run prepare "$LAUNCH_ID" \
  --output "$PACKET_DIRECTORY"
```

The default launch requires configured W&B-compatible ingress. Set
`TITLES_WANDB_INGRESS_BASE_URL` to an origin reachable from the trainer and route
the SDK endpoints to the ingress, not just `/api` to the operator API.
See [routing and SDK requirements](WANDB_COMPATIBILITY.md).
`--offline` is an explicit archival-only choice: object-storage backup alone
does not populate loss graphs or the run's samples.

Before starting the trainer:

- Verify the launch's project, dataset version, run ID, and preflight result.
- Verify `checksums.json`; keep `trainer.env` mode `0600`, never print or commit it.
- Copy the packet over an encrypted channel and extract the dataset.
- Install the documented pinned SDK and merge/use the packet's trainer config.
- Provide the S3 credentials named in `manifest.json`.
- Export the environment into both child processes with
  `set -a; . ./trainer.env; set +a`; never print the secret-bearing file.
- Run the included sync helper beside exactly one trainer execution.

After the first logging and sampling intervals, verify actual evidence:

```bash
mfiche --json --workspace "$WORKSPACE_ID" run live "$RUN_ID"
mfiche --json --workspace "$WORKSPACE_ID" run metrics "$RUN_ID"
mfiche --json --workspace "$WORKSPACE_ID" run samples "$RUN_ID"
mfiche --json --workspace "$WORKSPACE_ID" run checkpoints "$RUN_ID"
```

A connected run has real scalar steps and samples, not only a heartbeat.
Checkpoint upload and telemetry are separate paths. An empty sample list before
the configured first sample interval is not itself a failure. If data remains
absent, inspect the trainer logger/environment, ingress routing and SDK version,
and worker health; do not create another launch as a workaround.

## Diagnosing disconnected images

```bash
mfiche --json --workspace "$WORKSPACE_ID" asset show "$ASSET_ID"
mfiche --json --workspace "$WORKSPACE_ID" asset locations "$ASSET_ID"
mfiche --json --workspace "$WORKSPACE_ID" asset metadata "$ASSET_ID"
mfiche asset repair-images --help
```

Distinguish a missing durable original from an expired delivery URL, unavailable
remote credentials, and a missing thumbnail. Preserve asset IDs and lineage;
do not delete/reimport just to clear a broken preview. The project-scoped
`asset repair-images` operation is a durable repair job, not proof of successful
recovery until its terminal result is checked. Embedded metadata cannot be
extracted from remote bytes that have not been downloaded; never invent prompts
or dimensions from filenames.

## Single-copy storage maintenance

Do not create a full installation tarball merely to copy S3-backed originals
back into the same object store. First audit the selected workspace and source:

```bash
mfiche --workspace "$WORKSPACE_ID" --json storage single-copy audit --source "$SOURCE_ID"
mfiche --workspace "$WORKSPACE_ID" --json storage single-copy ensure-remote --source "$SOURCE_ID"
```

`ensure-remote` is a dry-run unless `--apply` is explicit. It skips assets that
already have an active, size- and SHA-256-verified S3 location. Applied uploads
use deterministic content-addressed keys under the source's managed prefix, so
retries reuse the same object. Stop Modelfiche before applying either storage
mutation:

```bash
mfiche stop
mfiche --workspace "$WORKSPACE_ID" --json storage single-copy ensure-remote --source "$SOURCE_ID" --apply
mfiche --workspace "$WORKSPACE_ID" --json storage single-copy evict-local --apply
```

`evict-local` deletes only paths inside configured asset/cache roots after an
active S3 source proves matching size and SHA-256. Remote-only exports and
caption reads fetch those verified objects instead of silently omitting them.
Keep the source credential environment available when restarting the API and
worker.

For database-only recovery state:

```bash
mfiche --json backup create --database-only
mfiche backup restore "/path/to/database-backup.tar.gz" \
  --database-only \
  --confirm "RESTORE MODELFICHE BACKUP"
```

Database-only archives do not contain asset bytes. Store them off-machine, and
delete the temporary local archive only after remote size and checksum
verification.

## Review and destructive operations

Ratings, production decisions, archive actions, disconnects, deletes, and
restore operations can affect downstream workflows. Read the target immediately
before mutation and honor every consequence-review or confirmation field.

Never synthesize confirmation phrases. The caller must explicitly authorize a
destructive operation. Backup restore additionally requires Modelfiche to be
stopped and the exact confirmation contract shown by command help.

## Secret handling

Never place these values in prompts, logs, issue bodies, commits, or normal
stdout:

- API keys and S3 credentials;
- signing private keys;
- `trainer.env` contents;
- `WANDB_API_KEY` or `MODELFICHE_HANDOFF_TOKEN`;
- presigned or capability URLs;
- unredacted support bundles;
- private dataset captions or images unless the task explicitly requires them.

Use file paths or environment-variable names in agent messages, not values.
Review redacted support bundles before sharing because domain content can still
be sensitive even when credential patterns are removed.

## Compatibility rules

- The installed `mfiche --help` output is canonical for command spelling.
- `mfiche --json capabilities` is canonical for feature availability.
- `mfiche --json enums` is canonical for enum values.
- OpenAPI is canonical for the local network schema.
- Additive JSON fields are compatible; missing required fields are not.
- Do not use commands copied from historical plans without checking help.

See [CLI.md](CLI.md) for operator workflows and [../CLI_SPEC.md](../CLI_SPEC.md)
for the stable behavior contract.

## Browser navigation and review

The global + menu carries the selected project into import and generation
forms. Workspace search remains available on mobile and supports arrow-key
selection. Gallery's search and project controls stay visible; optional image
sets, dataset membership, model, rating, and display controls live under
**More filters and display**.

Gallery filters, page, and page size are written to the workspace-scoped URL.
Use that URL to bookmark or share a review context. Closing an image returns to
its filtered results or the originating object. Object navigation starts a
separate editor state; save captions and add notes before leaving the object.
A failed form submission preserves its input and permits retry.
