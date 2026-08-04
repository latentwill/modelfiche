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

## Bootstrap sequence

Resolve the command once, then inspect runtime capabilities:

```bash
MFICHE="${MFICHE:-mfiche}"

"$MFICHE" --json version
"$MFICHE" --json capabilities
"$MFICHE" --json enums
"$MFICHE" --json doctor
```

If `mfiche` is not installed in a development checkout, set
`MFICHE="uv run mfiche"` in the calling environment or invoke that form
directly. Do not create a shell alias inside an automation script.

For unattended operations, add explicit context before the command group:

```bash
mfiche \
  --json \
  --quiet \
  --workspace "$WORKSPACE_ID" \
  --profile "$PROFILE_ID" \
  project list
```

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

The default trainer transport is outbound object storage:

```bash
mfiche --json training doctor
mfiche --json training run preflight "$LAUNCH_ID"
mfiche --json training run prepare "$LAUNCH_ID" \
  --output "$PACKET_DIRECTORY"
```

Before transfer:

- verify `checksums.json`;
- ensure `trainer.env` remains mode `0600`;
- do not print or commit the environment;
- copy the packet over an encrypted channel;
- provide only the S3 credentials named in `manifest.json`;
- run the included sync helper beside the selected trainer.

Observe through `run live`, `run metrics`, `run samples`, and `run checkpoints`.
No inbound tunnel is needed for checkpoint handoff.

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
