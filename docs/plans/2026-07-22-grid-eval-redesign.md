# Grid/Eval Redesign Contracts

**Version:** `2026-07-22.v1`
**Contract status:** implementation contract; version is persisted with every experiment plan and digest.

## Global rules

- `Project` is the sole global context. Every Grid, Eval, experiment plan, target, case, cell, run, admission, and output is owned by exactly one project.
- New writes do not accept or require `PromptSet`; historical PromptSet-backed definitions are backfilled to immutable inline snapshots. PromptSet write routes return `405`.
- Immutable snapshots are copied at write time. Later case, target, endpoint, provider, model, checkpoint, or parameter changes do not change a persisted plan.
- Every mutating request has an idempotency key scoped to project and route; grid queue keys are additionally scoped by grid ID. Coordinates and ordinals are unique within their owning grid/run.
- Adapter schemas, defaults, endpoint registrations, checkpoint revisions, and LoRA paths are server-owned. Clients may provide only target identity and bounded `lora_scale`; server hydration injects registered LoRA paths after rejecting spoofed fields.

## Immutable case contract

```json
{
  "case_id": "case_<opaque>",
  "ordinal": 0,
  "input": {"prompt": "...", "variables": {}},
  "input_digest": "sha256:<hex>"
}
```

`input` is inline and immutable. `case_id`/`ordinal` are stable inside the experiment plan; `input_digest` is canonical JSON SHA-256.

## Target contract

```json
{
  "target_id": "target_<opaque>",
  "ordinal": 0,
  "provider": "fal",
  "endpoint_id": "ideogram/v4/lora",
  "model_version_id": "model_version_<opaque>",
  "checkpoint_revision_id": "checkpoint_<opaque>",
  "schema_digest": "sha256:<hex>",
  "schema": {
    "request_fields": {},
    "defaults": {},
    "fields": {},
    "grid": {}
  },
  "fixed_target": {
    "provider": "fal",
    "endpoint_id": "ideogram/v4/lora",
    "model_version_id": null,
    "checkpoint_revision_id": null
  },
  "shared_overrides": {},
  "target_overrides": {}
}
```

`provider` is always `fal` for the current backend. Ideogram and Krea are endpoint IDs (for example `ideogram/v4/lora` and `fal-ai/krea-2/turbo/lora`) and may be mixed in one plan. `schema` is the existing adapter contract returned by `grid_contract()` (`request_fields`, `defaults`, `fields`, and `grid`), not a generic JSON Schema. `model_version_id` and `checkpoint_revision_id` are optional; `fixed_target` is the immutable target identity bundle. Target adapter schema and overrides may differ per endpoint.

## Axis-first experiment plan

```json
{
  "contract_version": "2026-07-22.v1",
  "project_id": "project_<opaque>",
  "plan_id": "plan_<opaque>",
  "plan_version": 1,
  "digest": "sha256:<hex>",
  "axes": {
    "x": {"name": "...", "values": [{"value": {}, "digest": "sha256:<hex>"}]},
    "y": {"name": "...", "values": [{"value": {}, "digest": "sha256:<hex>"}]},
    "z": null
  },
  "cases": [],
  "targets": []
```

Axes are ordered and must include X and Y; Z is optional. Axis values and all snapshots are canonicalized before computing `digest`. Effective parameter precedence is endpoint default < shared params < target override < axis value (later values win at each key).

Axis semantics are explicit and cardinality is exact:

- `x`, `y`, and optional `z` are the only comparison dimensions. Axis values are ordered and unique.
- A dimension named `target` contains target IDs and selects those IDs; it does **not** multiply by every entry in `targets`.
- A dimension named `prompt` or `case` contains case IDs and selects those IDs; it does **not** multiply by every entry in `cases`.
- Any other axis is a parameter dimension. Targets and cases are fixed context unless they are explicitly represented as comparison dimensions. A plan MUST declare `fixed_target_id` and `fixed_case_id` when target/case is not an axis; these IDs MUST exist in `targets`/`cases`.

- Model/checkpoint comparisons use the `target` axis with target IDs and immutable target bundles. `model` and `checkpoint_step` are not adapter request axes; treating either as a parameter axis is invalid because it would be rejected by the endpoint request schema.
- Resolved cardinality is `|x.values| × |y.values| × (|z.values| or 1) × |selected_cases| × |selected_targets|`, where selected cases/targets are one each for fixed context, and axis-selected sets otherwise. Thus X=`target` with 2 IDs and Y=`prompt` with 2 IDs resolves exactly 4 cells.

## Resolved preflight cell contract

```json
{
  "ordinal": 0,
  "coordinate": {"x": 0, "y": 0, "z": null},
  "case": {},
  "target": {},
  "effective_params": {},
  "request_count": 1,
  "estimated_cost": "0.000000",
  "schema_digest": "sha256:<hex>",
  "status": "admissible"
}
```

Preflight deterministically emits cells ordered by `ordinal` (row-major X/Y/Z and stable case/target order), validates each effective parameter object against its target schema, reports exact request counts/costs, and returns stable schema errors without enqueueing.

## Persisted grid/run/eval contract

- Grid and GridCell persist the plan snapshot/digest, inline cases, target identity, endpoint adapter/schema digest, coordinate, effective params, status, output reference/payload, request/cost accounting, and idempotency key.
- Cell statuses are `pending|admitted|running|succeeded|failed|cancelled`; admissions are per-cell child admissions under one parent operation and are idempotent.
- Eval references the same `plan_id`/`plan_version`/`digest` and generation `run_id`; it stores only assessment configuration/results, never a duplicate generation configuration.
- Eval assessment kinds are `human_review` and `image_quality`; assessment statuses are `ready_for_review|queued|running|succeeded|failed`. A completed generation run is required before an assessment can be created.

## Backfill and migration contract

Existing PromptSet-backed GridDefinition/EvalDefinition rows are migrated in place to immutable inline case snapshots and target/plan snapshots. Historical columns remain readable for historical records where required, but new create/update paths reject PromptSet input and use only project-owned snapshots. A plan version and digest are written for every backfilled row.

## Stable HTTP routes and exact shapes
All routes below are under `/api`. Request IDs are UUID strings; `idempotency_key` is required on every mutating route and is scoped to `project_id` plus route. Grid queue durability uses `grid:{project_id}:{grid_id}:{idempotency_key}` for both the parent job and queue item. Responses use exact persisted snapshots and never include a PromptSet field.

### Plan preflight

`POST /projects/{project_id}/experiment-plans/preflight`

Request:

```json
{
  "contract_version": "2026-07-22.v1",
  "idempotency_key": "preflight-opaque",
  "axes": {
    "x": {"name": "seed", "values": [5, 7]},
    "y": {"name": "image_size", "values": ["square_hd", "portrait_4_3"]},
    "z": null
  },
  "cases": [
    {"case_id": "case-a", "ordinal": 0, "input": {"prompt": "a"}}
  ],
  "targets": [
    {
      "target_id": "target-ideo",
      "ordinal": 0,
      "provider": "fal",
      "endpoint_id": "ideogram/v4/lora",
      "model_version_id": null,
      "checkpoint_revision_id": null,
      "overrides": {}
    }
  ]
}

Response:

```json
{
  "contract_version": "2026-07-22.v1",
  "project_id": "project_<opaque>",
  "plan_version": 1,
  "digest": "sha256:<hex>",
  "valid": true,
  "schema_errors": [],
  "cells": [
    {
      "ordinal": 0,
      "coordinate": {"x": 0, "y": 0, "z": null},
      "case": {"case_id": "case-a", "ordinal": 0, "input": {"prompt": "a"}, "input_digest": "sha256:<hex>"},
      "target": {"target_id": "target-ideo", "ordinal": 0, "provider": "fal", "endpoint_id": "ideogram/v4/lora", "schema_digest": "sha256:<hex>"},
      "effective_params": {"seed": 5, "image_size": "square_hd", "num_images": 1},
      "request_count": 1,
      "estimated_cost": "0.000000",
      "schema_digest": "sha256:<hex>",
      "status": "admissible"
    }
  ],
  "request_count": 4,
  "estimated_cost": "0.000000"
}
```

### Grid create/get/list

`POST /projects/{project_id}/grids`

Request:

```json
{
  "name": "grid-name",
  "idempotency_key": "grid-create-opaque",
  "plan": {
    "contract_version": "2026-07-22.v1",
    "axes": {"x": {"name": "seed", "values": [5, 7]}, "y": {"name": "image_size", "values": ["square_hd"]}, "z": null},
    "cases": [{"case_id": "case-a", "ordinal": 0, "input": {"prompt": "a"}}],
    "targets": [{"target_id": "target-ideo", "ordinal": 0, "provider": "fal", "endpoint_id": "ideogram/v4/lora", "overrides": {}}]
  }
}
```

Response `201` (resolved cells are persisted at create time):

```json
{
  "id": "grid_<opaque>",
  "project_id": "project_<opaque>",
  "name": "grid-name",
  "plan": {"contract_version": "2026-07-22.v1", "plan_version": 1, "digest": "sha256:<hex>", "axes": {"x": {"name": "seed", "values": [5, 7]}, "y": {"name": "image_size", "values": ["square_hd"]}, "z": null}, "cases": [{"case_id": "case-a", "ordinal": 0, "input": {"prompt": "a"}}], "targets": [{"target_id": "target-ideo", "ordinal": 0, "provider": "fal", "endpoint_id": "ideogram/v4/lora"}]},
  "plan_version": 1,
  "plan_digest": "sha256:<hex>",
  "status": "draft",
  "cells": [
    {"ordinal": 0, "coordinate": {"x": 0, "y": 0, "z": null}, "effective_params": {"seed": 5, "image_size": "square_hd"}},
    {"ordinal": 1, "coordinate": {"x": 1, "y": 0, "z": null}, "effective_params": {"seed": 7, "image_size": "square_hd"}}
  ],
  "request_count": 2,
  "estimated_cost": "0.000000"
}
```

`GET /projects/{project_id}/grids` returns `{"items":[<grid response>],"next_cursor":null}`. `GET /projects/{project_id}/grids/{grid_id}` returns the grid response with all deterministic resolved cells. Grid IDs not owned by the project return `404`.


`POST /projects/{project_id}/grids/{grid_id}/queue`
Request: `{"idempotency_key":"grid-queue-opaque","plan_version":1,"plan_digest":"sha256:<hex>","cell_ordinals":[0,1],"billing_acknowledgement":"I understand FAL may bill this run even if checkpoint fetch fails"}`

Response `202`:

```json
{"grid_id":"grid_<opaque>","run_id":"run_<opaque>","admission_id":"admission_<opaque>","status":"queued","plan_version":1,"plan_digest":"sha256:<hex>","request_count":2,"child_admissions":[{"ordinal":0,"cell_id":"cell_<opaque>","admission_id":"child_<opaque>","status":"admitted"}]}
```
Repeated requests with the same route-scoped idempotency key return the identical response and never double-bill or enqueue duplicate children. Billing gates use the literal acknowledgement `I understand FAL may bill this run even if checkpoint fetch fails`; gates are evaluated before parent or child admission persistence.

### Eval create/reference-run

`POST /projects/{project_id}/evals`

Request:

```json
{
  "name": "assessment-name",
  "idempotency_key": "eval-create-opaque",
  "run_id": "run_<opaque>",
  "assessment": {"kind": "image_quality", "parameters": {}}
}
```

`run_id` references an existing Grid/Eval generation run whose immutable plan is reused. Response `201`:

```json
{
  "id":"eval_<opaque>",
  "project_id":"project_<opaque>",
  "run_id":"run_<opaque>",
  "plan_id":"plan_<opaque>",
  "plan_version":1,
  "plan_digest":"sha256:<hex>",
  "assessment":{"kind":"image_quality","parameters":{}},
  "status":"ready_for_review"
}
```

`POST /projects/{project_id}/evals` creates the assessment immediately in `ready_for_review` state after validating the completed generation run and immutable plan; no evaluator job or follow-up run route exists. `GET /projects/{project_id}/evals/{eval_id}` returns the response shape above plus assessment results.

## Owned implementation file plan

- `apps/api/titles_api/models.py`: immutable plan/case/target/grid-cell/run/eval-reference columns and ownership/coordinate constraints.
- `apps/api/titles_api/schemas.py`: versioned plan, target, axis, preflight, grid queue, and eval assessment create/response validation; reject PromptSet on new writes.
- `apps/api/titles_api/routers/grid_eval.py`: stable project-scoped grid/eval routes, preflight, idempotency, ownership, and snapshots.
- `apps/api/titles_api/storage/fal_admissions.py`, `routers/fal_admissions.py`, and `routers/generation_queue.py`: aggregate billing gates and per-cell child admissions.
- `apps/api/titles_api/integrations/fal/adapters.py` and `orchestration.py`: provider-specific schemas, mixed targets, precedence, and deterministic plan expansion.
- `apps/api/alembic/versions/0021_grid_eval_redesign.py`: additive schema migration plus PromptSet backfill and historical preservation.
- `apps/worker/titles_worker/fal_jobs.py`, `fal_request_resolver.py`, `sqlalchemy_eval_sink.py`, and `main.py`: consume only persisted plan/cell snapshots and write target-aware outputs/statuses.
- `apps/api/tests/test_grid_eval_redesign.py` and `apps/api/tests/test_database_migrations.py`; worker coverage is in `apps/worker/tests/test_fal_grid_batch.py`, `test_sink_grid_status.py`, and `test_sqlalchemy_eval_sink.py`: contract, migration, admission, worker, idempotency, ownership, and mixed-provider coverage.
