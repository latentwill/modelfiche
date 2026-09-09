# W&B-Compatible Trainer Logging

Modelfiche implements the narrow W&B 0.28.0 protocol surface used by AI Toolkit and kef-krea2. It accepts live scalar and text metrics, configuration, image samples and captions, run resume, and run completion. It is not a general W&B server.

No wandb.ai account or vendor API key is used. `WANDB_API_KEY` is only the
environment-variable slot that the pinned SDK uses to build its HTTP Basic
header; Modelfiche fills that slot with its own run-scoped `MF1_...` launch
token and directs all traffic to `WANDB_BASE_URL`.

## Compatibility contract

| Item | Contract |
| --- | --- |
| W&B SDK | Exactly `wandb==0.28.0` |
| Trainer | AI Toolkit `WandbLogger` or kef-krea2's Modelfiche telemetry adapter |
| Run ownership | The DAM creates the launch and expected run before the trainer connects |
| Authentication | Modelfiche run-scoped Ed25519 `MF1_...` token carried in the SDK's `WANDB_API_KEY` compatibility slot; no wandb.ai key |
| Metrics | Scalar numbers and text values, persisted by step in the database |
| Media | Images and captions only |
| Artifacts | Images, the pinned SDK's allowlisted configuration files, and manifest-declared checkpoints in S3 |
| Realtime transport | Server-Sent Events from the DAM API to the browser |

Supported trainer calls:

```python
run = wandb.init(project=project, name=name, config=config)
wandb.log(metrics, commit=False)
wandb.log({}, step=step, commit=True)
wandb.log({"sample": wandb.Image(image, caption=caption)})
run.finish()
# kef-krea2 uses the equivalent run-scoped call:
run.log(metrics, step=step)
```

Unsupported GraphQL documents, file paths, media types, sweeps, reports, tables, artifact registries, model registries, and generic W&B integrations fail explicitly. Do not point unrelated W&B integrations at this service.

## Services and routing

Run the regular API, worker, and W&B ingress against the same database:

```bash
export TITLES_DATABASE_URL='postgresql+psycopg://modelfiche:...@db/modelfiche'
export TITLES_WANDB_INGRESS_DATABASE_URL="$TITLES_DATABASE_URL"
export TITLES_WANDB_INGRESS_BASE_URL='https://dam.example.com'

mfiche start
mfiche status
```

`mfiche start` supervises the operator API, W&B compatibility ingress, worker,
and web UI as one stack. If any process exits, the stack is no longer healthy.

The ingress listens on `127.0.0.1:8401` by default. Override it with `TITLES_WANDB_INGRESS_HOST` and `TITLES_WANDB_INGRESS_PORT`.

The public reverse proxy must route W&B SDK paths to the ingress service:

```text
/graphql                                  -> W&B ingress
/files/{entity}/{project}/{run}/...       -> W&B ingress
/wandb-upload/{upload_id}                 -> W&B ingress
/checkpoint-handoffs/{run_id}             -> W&B ingress
/api/...                                  -> regular DAM API
```

New launches default to live W&B-compatible telemetry and require
`TITLES_WANDB_INGRESS_BASE_URL`. Use an origin reachable from the trainer and
route the SDK paths above to the ingress service. The operator API's port is
not an ingress fallback. Configuration readiness is not proof of trainer-side
network reachability: verify real scalar and sample events after starting.

For intentional archival-only training, pass `--offline` to
`training run create` or `"live_telemetry": false` in the launch JSON. This
backs up trainer output but does not ingest loss graphs or samples from offline
W&B files. Multi-process live-ingress deployments should use Postgres.

The examples below use the installed `mfiche` command. In a checkout without
that entry point on `PATH`, use the equivalent `uv run mfiche ...` command.

## One-time global signing-key setup

Modelfiche uses one Ed25519 signing key for all workspaces and projects. Run
the guided setup once:

```bash
mfiche --json training setup
```

The command creates
`$HOME/.config/modelfiche/ai-toolkit-signing.key` with mode `0600` and registers
only its public key. If a different global public key is already registered,
the command rotates it to the local key instead of requiring a project- or
workspace-specific credential.

To manage the key manually, generate a private key and register its public
key:

```bash
mfiche --json wandb key generate \
  --private-key-output "$HOME/.config/modelfiche/ai-toolkit-signing.key"
mfiche --json wandb credential register --public-key <public-key>
```

Recover the public key from an existing private key without contacting
Modelfiche:

```bash
mfiche --json wandb key public \
  --private-key-file "$HOME/.config/modelfiche/ai-toolkit-signing.key"
```

Store the private-key file in the operator's secret manager or encrypted
trainer provisioning system. Never include it in launch JSON, environment
logs, repository files, container images, or API requests. Rotation installs
a new global public key and invalidates tokens signed by the previous key.

## Create a training launch

A launch automatically uses the one global W&B signing key. It binds one
dataset version, one writable S3 source, one trainer (`ai-toolkit` or
`kef-krea2`), and one expected Modelfiche run. Use a caller-generated
`client_request_id`; retrying the same request is idempotent. Create one
Modelfiche launch per kef-krea2 grid member so each Modelfiche run has one
monotonic step sequence.

Pass `--workspace <id-or-slug>` to every command below; use the workspace that
owns the dataset and project. The API rejects cross-workspace bindings. The UI's
workspace selection does not change CLI context.


Create `launch.json`:

```json
{
  "dataset_version_id": "<dataset-version-id>",
  "source_id": "<writable-s3-source-id>",
  "client_request_id": "agent-job-2026-07-13-001",
  "name": "portrait-lora-v4",
  "trainer": "ai-toolkit",
  "base_model": "black-forest-labs/FLUX.1-dev",
  "output_directory": "/workspace/output",
  "checkpoint_policy": {
    "every_n_steps": 1000
  },
  "backup_policy": {
    "interval_seconds": 300
  },
  "supported_endpoint_ids": [],
  "expected_duration_seconds": 86400
}
```

Then create the launch:

```bash
mfiche --json \
  --request-id agent-job-2026-07-13-001 \
  training-launch create --input launch.json
```

The response contains `id`, `run_id`, launch state, manifest digest, run prefix, export job identity, backup state, checkpoint handoff state, and the redacted launch manifest. Agents should persist `id`, `run_id`, `client_request_id`, and `manifest_digest`.

The same fields can be supplied as flags. Use `mfiche training-launch create --help` for the complete flag contract.

## Produce the trainer packet

The recommended command downloads the immutable dataset export and writes it
beside the manifest, signed environment, trainer-specific logging metadata,
checksums, and checkpoint-sync helper:

```bash
mfiche --json training run prepare <launch-id> --output ./trainer-packet
```

The packet always uses outbound object storage for checkpoints and recovery
files. Connected launches additionally set `WANDB_BASE_URL`, `WANDB_ENTITY=dam`,
`WANDB_MODE=online`, and the signed run identity. This overrides a stale offline
SDK mode in the trainer's parent environment. Explicit offline packets set
`WANDB_MODE=disabled` and disable the trainer logger.

Dataset downloads use the selected operator API, independently of the telemetry
origin. Copy the packet, verify `checksums.json`, extract `training-dataset.zip`,
provide the named S3 credentials, export `trainer.env`, and run
`modelfiche-run-sync.py` beside the selected trainer. The trainer needs outbound
access to S3 and, for losses and samples, W&B ingress.

For deployments that explicitly enable live W&B telemetry, the ordinary
environment endpoint intentionally contains a token placeholder:

```bash
mfiche training-launch environment <launch-id>
```

Render a ready-to-source environment locally by signing the launch's canonical claims:

```bash
mfiche --json training-launch agent-environment <launch-id> \
  --private-key-file "$HOME/.config/modelfiche/ai-toolkit-signing.key" \
  --output /run/secrets/modelfiche-training.env
```

The helper:

1. fetches the redacted launch manifest and environment template;
2. reads the private key locally;
3. signs the exact canonical claims expected by the ingress;
4. writes the completed environment with mode `0600`;
5. prints metadata about the output file without printing the token.

The resulting file contains:

```text
WANDB_BASE_URL=https://dam.example.com
WANDB_ENTITY=dam
WANDB_MODE=online
WANDB_PROJECT=<expected-project-name>
WANDB_RUN_ID=<expected-wandb-run-id>
WANDB_RESUME=allow
WANDB_API_KEY=<Modelfiche-MF1-run-scoped-signed-token>
MODELFICHE_HANDOFF_TOKEN=<same-Modelfiche-token>
```

Source it only in the trainer process:

```bash
set -a
. /run/secrets/modelfiche-training.env
set +a
python run.py <ai-toolkit-config.yaml>
```

For Krea 2 embeddings, create the launch with `"trainer": "kef-krea2"` or use
the editable CLI settings:

```bash
mfiche training run create \
  --dataset <dataset-id-or-name> \
  --name <launch-name> \
  --trainer kef-krea2 \
  --base-model krea/Krea-2-Raw \
  --steps 8000 \
  --tokens 5 \
  --gradient-accumulation 4 \
  --sample-interval 250 \
  --caption-mode random \
  --model-revision <RAW_MODEL_COMMIT>
```

Use `--caption-mode none` when the Krea run must ignore captions. This changes
only Krea conditioning; the W&B telemetry contract is unchanged.

The generated `kef-krea2-training.json` records the validated launch
configuration and exact `kef-krea2-train` argument vector. Install the pinned
`wandb==0.28.0` training extra, extract the packet dataset into
`training-dataset`, source `trainer.env`, then start that recorded command.

Calling `agent-environment` without `--output` writes the secret-bearing shell environment to stdout. This is intentional for process substitution, but agents should prefer a `0600` file to avoid accidental transcript capture.

The global key signs a token that identifies its workspace, project, launch,
Modelfiche run, and W&B run ID. It also has issued-at and expiry claims. An
invalid signature, expired token, rotated key, or revoked key is rejected as
unauthenticated.

## Agent-oriented observation commands

All observation commands support the global `--json`, `--quiet`, `--profile`, `--workspace`, `--request-id`, and `--timeout` options.

Return the launch contract and current live run state in one JSON document:

```bash
mfiche --json training-launch inspect <launch-id>
```

The result has stable top-level `launch` and `run` objects. The live run object includes status, current step, latest loss, learning rate, elapsed seconds, last-event time, metric summaries, upload counts, and sample count.

Wait for a terminal launch state:

```bash
mfiche --json --quiet training-launch wait <launch-id> \
  --timeout 86400 \
  --poll-interval 5
```

Exit behavior:

- `0`: launch completed;
- `7`: launch failed or was interrupted;
- structured `TRAINING_LAUNCH_WAIT_TIMEOUT`: timeout elapsed and the caller may retry.

Read the current live projection directly by DAM run ID:

```bash
mfiche --json run live <run-id>
```

Read one ordered scalar series:

```bash
mfiche --json run metrics <run-id> --name loss/denoise
mfiche --json run metrics <run-id> --name learning_rate
```

Inspect samples, checkpoints, configuration, and lineage:

```bash
mfiche --json run samples <run-id> --limit 20
mfiche --json run checkpoints <run-id>
mfiche --json run config <run-id>
mfiche --json run lineage <run-id>
```

For shell agents, parse JSON output rather than scraping human text. Diagnostics and wait progress go to stderr; `--quiet` suppresses wait progress.

## Storage layout

Metrics and live state are stored in the relational database. Large objects use the launch's S3 source and run prefix:

```text
projects/{project_id}/runs/{run_id}/
  samples/
  configs/
  checkpoints/
```

The W&B upload allowlist is exact: image files below `media/images/` plus
`config.yaml`, `requirements.txt`, `wandb-metadata.json`, and
`wandb-summary.json`. Generic log upload is not part of the compatibility shim;
metric history remains in the database.

Image samples are attached to the run only after upload reconciliation verifies object size and SHA-256. A visible upload can therefore progress through pending, uploaded, verified, or failed states.

Checkpoint publication is manifest-last. Upload checkpoint objects first, upload the generation-specific manifest last, then call the manifest callback from the launch contract. The worker validates manifest identity, exact object versions when available, declared sizes, and SHA-256 values before creating checkpoints and candidate model versions.

## Reliability behavior

The shim handles:

- idempotent launch and run creation;
- duplicate metric batches;
- out-of-order steps;
- reconnect and resume;
- repeated completion calls;
- interrupted upload reconciliation;
- generation-fenced checkpoint handoffs;
- stale-run interruption.

A running run with no events for `TITLES_WANDB_STALE_AFTER_SECONDS` is marked interrupted by the maintenance worker. The default is 900 seconds.

Ingress limits are configurable:

```text
TITLES_WANDB_INGRESS_RATE_LIMIT_PER_MINUTE
TITLES_WANDB_GRAPHQL_MAX_BYTES
TITLES_WANDB_FILESTREAM_MAX_BYTES
TITLES_WANDB_IMAGE_MAX_BYTES
TITLES_WANDB_CONFIG_MAX_BYTES
TITLES_WANDB_STALE_AFTER_SECONDS
```

Rate-limited responses include `Retry-After`. The pinned W&B SDK buffers locally and retries transient transport failures.

## Troubleshooting

### `401 Modelfiche launch token is required; a wandb.ai API key is not used`

The trainer did not send the generated Modelfiche token through the SDK's
`WANDB_API_KEY` compatibility slot, or a proxy removed the `Authorization`
header. Confirm the rendered environment was sourced by the trainer process
and preserve HTTP Basic authentication through the reverse proxy. Do not add a
wandb.ai API key.

### `401 malformed Modelfiche launch token`

The environment still contains the placeholder, the token was truncated, or
output was transformed. Regenerate it with `training-launch
agent-environment`.

### `401 Modelfiche launch token signature is invalid`

The local private key does not match the public key registered in Modelfiche,
or the signing credential was rotated. Compare `wandb key public` with the
registered credential and create a new launch token.

### `409 training launch is not accepting W&B events`

The launch is not in a state that accepts telemetry. Inspect it with `training-launch inspect`; do not reuse a failed or interrupted launch.

### Trainer project or run name mismatch

AI Toolkit must use the project, run ID, and resume policy from the generated environment. Do not override `WANDB_PROJECT`, `WANDB_RUN_ID`, or `WANDB_RESUME` later in the startup script.

### Metrics appear but samples remain pending

Confirm the worker is running, the launch's S3 source is writable, and its credentials are available to the worker. Inspect `run live`, `run samples`, and worker logs. A failed digest or size verification is terminal for that upload generation; a transport failure remains retryable.

### Run remains running after trainer loss

The maintenance worker marks it interrupted after the stale threshold. Use `run live` to check `last_event_at`; do not mark it complete manually unless the trainer actually finished.

## Compatibility verification and upgrades

Run the pinned protocol and end-to-end tests:

```bash
uv run --extra compat-wandb python -m pytest -q \
  tests/compat/wandb/0.28.0/test_recorder.py \
  tests/compat/wandb/0.28.0/test_dam_compat.py
```

A W&B SDK upgrade is a protocol change, not a routine dependency bump:

1. create a new versioned fixture directory;
2. capture canonical and AI Toolkit request streams;
3. inspect every new GraphQL document, route, header, and response shape;
4. update the narrow protocol implementation;
5. approve new document fingerprints;
6. run the S3 image and completion compatibility test;
7. update this guide and the exact dependency pin.

Never update the W&B version without regenerating and reviewing the compatibility fixtures.
