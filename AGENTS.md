# Agent Rules

## Model and provider

- All subagents for this project MUST run through the OpenAI Codex runtime.
- Subagents MUST NOT use OpenRouter, Claude, Anthropic, Gemini, or any other external model/provider routing.
- Do not invoke external AI CLIs or provider APIs for implementation, review, testing, or research.

## Delegation

- Keep delegated work scoped to non-overlapping files and verify agent-reported changes independently before claiming completion.

## Product UI constraints

- NEVER add user-facing billing acknowledgements, billing-review checkboxes, cost-review gates, or billing-warning confirmation dialogs to Modelfiche UI or CLI flows.
- Provider billing and failure risk are system concerns; generation and queue actions MUST NOT require a user checkbox or acknowledgment phrase before submission.

## Start here

- Modelfiche is the product name; Python packages still use `titles_api`,
  `titles_worker`, and `titles_cli`. The supported executable is `mfiche`.
- Operating the app: read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md), then run
  `uv run mfiche --json capabilities`, `uv run mfiche --json workspace list`,
  and the relevant command's `--help`. Use `uv run` in this checkout so an
  older globally installed CLI cannot silently define the contract.
- Pass `--workspace <id-or-slug>` before the command group on reads AND writes.
  Unscoped reads select the oldest workspace, not necessarily the UI workspace.
  Mutations require an explicit workspace header; do not guess names or create
  replacement projects when a scoped lookup returns no result.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) for verification commands. Current
  source, OpenAPI (`/openapi.json`), and CLI help take precedence over historical plans.

## Code map

| Change | Start here |
| --- | --- |
| CLI flags, JSON output, packet preparation | `apps/cli/titles_cli/main.py`, `client.py` |
| Workspace ownership and request scope | `apps/api/titles_api/services.py`, `routers/core.py` |
| Training manifests, preflight, trainer config | `apps/api/titles_api/training_launches.py`, `routers/training_launches.py` |
| W&B auth, SDK protocol, uploads, metrics | `apps/api/titles_api/wandb_*.py`, `training_metrics.py` |
| Trainer backups and checkpoint handoff | `apps/cli/titles_cli/run_sync.py`, `apps/worker/titles_worker/checkpoint_handoffs.py` |
| Image imports and embedded metadata | `apps/api/titles_api/routers/local_imports.py`, `integrations/s3/`, `image_provenance.py` |
| Image delivery and hydration | `apps/api/titles_api/routers/asset_delivery.py`, `storage/`, `apps/worker/titles_worker/hydration_jobs.py` |
| Durable jobs and repair | `apps/worker/titles_worker/runner.py`, `import_jobs.py`, `image_lineage_jobs.py` |
| Browser screens and API client | `apps/web/src/`, especially `api.ts` and `docs-screen.tsx` |
| Schema and migrations | `apps/api/titles_api/models.py`, `schemas.py`, `apps/api/alembic/versions/` |
| Regression coverage | `apps/api/tests/`, `apps/cli/tests/`, `apps/worker/tests/`, `tests/compat/`, `tests/integrations/` |

## Workflow completion criteria

- Training is not connected merely because a launch or packet exists. Verify
  workspace/project/dataset ownership, preflight, trainer environment, and
  actual `run metrics`, `run samples`, and `run checkpoints` for the returned
  run ID. See [docs/WANDB_COMPATIBILITY.md](docs/WANDB_COMPATIBILITY.md).
- Object-storage backup is not live telemetry. Never claim loss graphs or
  samples are connected from successful checkpoint upload alone.
- Image imports must preserve durable bytes or a verified remote location,
  metadata, and dataset/run lineage. Inspect `asset locations` and
  `asset metadata`; do not substitute a temporary URL for durable storage.
- Do not repair user data by editing SQLite or moving `var/assets` manually.
  Use supported repair/import APIs and verify durable job results.
- Tests and smoke checks must use isolated temporary databases/storage.
  Never launch paid provider jobs or change the running user's database merely
  to verify code. Update the operator guide and in-app Docs for workflow changes.

Make code easy to follow. Make the job look easy.
