# Modelfiche system map (MAP-01)

Verified against `qa/map-01` on 2026-07-12. This is an implementation map, not a desired-state specification.

## Domain invariants for all follow-on packets

- A **Workspace** is the top-level container. A **Project** belongs to one workspace.
- A **Training Run** is an execution. A **Model Version / Checkpoint** is an artifact selected from that execution. Neither is a dataset.
- **DATASET** means an imported source image/caption represented by `DatasetVersion`/`DatasetItem` and its `Asset`.
- **SAMPLE** means an AI Toolkit training image represented by `Sample` and its `Asset`.
- **EVAL** means a FAL-generated evaluation image represented by `EvalOutput` and its `Asset`.
- Asset presentation metadata must retain distinct project, training-run, checkpoint/model, dataset, eval-run, and source/storage references. Do not flatten these into one ambiguous `source`, `run`, or `model` field.
- EVAL chronological order is FAL `generated_at`. SAMPLE chronological order is source object `modified_at`. Substituting `Asset.created_at`, `EvalOutput.created_at`, import time, or another timestamp is a contract violation.
- Any gallery/lightbox transition must round-trip the exact origin route, query, filters, pagination, selected item, and context.

## Runtime and package boundaries

| Layer | Entry points | Responsibility |
| --- | --- | --- |
| Web | `apps/web/src/main.tsx`, `App.tsx` | React/Vite hash-routed operator SPA |
| API | `apps/api/titles_api/__main__.py`, `app.py` | FastAPI, SQLite/SQLAlchemy, local security boundary |
| Worker | `apps/worker/titles_worker/main.py`, `runner.py` | Durable imports, refresh/hydration, exports, FAL eval/grid work |
| CLI | `apps/cli/titles_cli/main.py` | Operator/agent commands over API |
| Schema | `apps/api/titles_api/models.py`, `alembic/versions/` | ORM and six numbered Alembic revisions (`0001`-`0006`) |

The SPA uses `location.hash`, not React Router. `useHashRoute` in `hooks.ts` supplies the route; `App.tsx` splits it into section, one ID segment, and `URLSearchParams`. Most screens render inside `WorkbenchShell`; `image`, `samples`, `eval`, and `grid` bypass it as focus/lightbox workspaces.

The root HTML files (`project.html`, `gallery.html`, `eval-viewer.html`, `grid-lightbox.html`, etc.) and `design/` files are static prototypes/audit inputs, not runtime routes. Do not patch them in place of `apps/web/src/`.

## Frontend route and component map

| Hash route | Main component/file | Key API/data and navigation notes |
| --- | --- | --- |
| `#/dashboard[?create=project]` | `DashboardScreen`, `screens-wireframe.tsx` | projects/runs dashboard; inline `ProjectCreatePanel` |
| `#/projects` | `ProjectsScreen`, `screens-core.tsx` | list/create projects |
| `#/project/:id` | `ProjectScreen`, `screens-core.tsx` | project settings and project-scoped image panels |
| `#/import[?project=:id]` | `ImportScreen`, `screens-core.tsx` | select S3 source/project, browse/detect prefix, enqueue import; or import a browser-selected local folder with sidecars/progress |
| `#/jobs/:id[?job=1]` | `JobScreen`, `screens-core.tsx` | import job by default; general durable job with `job=1` |
| `#/datasets[?project=:id]` | `DatasetsScreen`, `screens-assets.tsx` | dataset list |
| `#/dataset/:id` | `DatasetScreen` + `CaptionItem`, `screens-assets.tsx` | versions, editable draft, caption operations, client-side pagination |
| `#/runs[?project=:id]` | `RunsScreen`, `screens-assets.tsx` | training executions |
| `#/run/:id` | `RunScreen`, `LossGraph`, `ConfigDetails`, `screens-assets.tsx` | config, metrics, checkpoints, samples; model registration |
| `#/checkpoint/:id` | `CheckpointScreen`, `screens-assets.tsx` | checkpoint artifact, hydration, promotion context |
| `#/models[?project=:id]` | `ModelsScreen`, `screens-assets.tsx` | model families |
| `#/model/:id` | `ModelScreen` + `ModelTrainingRun`, `screens-assets.tsx` | model versions/checkpoints and linked training runs |
| `#/gallery?...` | `GalleryScreen`, `GalleryImageOverlay`, `AssetGrid`, `screens-assets.tsx` | project/model/dataset/category/kind/decision/rating/search filters; server pagination; selected asset is `asset=` query parameter |
| `#/image/:asset-id` | `ImageReviewScreen`, `screens-wireframe.tsx` | standalone asset lightbox/review; reconstructs a broad project/kind gallery rather than exact origin |
| `#/samples/:run-id[?step=...]` | `SampleViewerScreen`, `screens-wireframe.tsx` | standalone multi-step SAMPLE comparison/lightbox |
| `#/prompt-sets[/:id][?project=:id]` | `PromptSetsScreen` + `PromptSetBuilder`, `screens-evals.tsx` | shared prompt CRUD and LLM generation |
| `#/evals[?project=:id]` | `EvalsScreen` + dynamic `EndpointFields`, `screens-evals.tsx` | evaluation generator/list; FAL admission flow |
| `#/eval/:id` | `EvalScreen`, `RatingControl`, `screens-evals.tsx` | standalone eval run/output viewer; bypasses shell |
| `#/grids[?project=:id]` | `GridsScreen`, `AxisValuesField`, `GridPreview`, `screens-evals.tsx` | grid generator/list |
| `#/grid/:id` | `GridScreen`, `screens-evals.tsx` | standalone grid lightbox/run admission |
| `#/reviews[?project=:id&subject=...]` | `ReviewsScreen`, `screens-evals.tsx` | generic review/comment screen |
| `#/transfers[?mode=import|export&...]` | `TransfersScreen`, `TransferImport`, `TransferExport`, `screens-wireframe.tsx` | import/export handoff and durable transfers |
| `#/settings` | `SettingsScreen`, `S3SourcesPanel`, `screens-core.tsx` | operator defaults, profiles, S3 sources and tests |

### Shell and shared frontend building blocks

- `WorkbenchShell`, `Topbar`, `Sidebar`, `ProjectTree`, `NavBranch`, `ContextInspector`, `NotesPanel` (`shell.tsx`) are shared by every non-focus route. The top bar shows the first workspace; it does not switch/create workspaces.
- `Page`, `Panel`, `Notice`, `Field`, `Form`, `Pagination`, `DataTable`, `SelectRecords`, `Status`, and `ProfileSelect` (`ui.tsx`) are shared selectors/cards/forms/status controls.
- `AssetGrid` is the common image-card collection used in gallery, run/model/project panels. `AssetImage` (`asset-image.tsx`) is the only runtime image URL resolver and should remain the image primitive.
- Generator controls are currently local to `screens-evals.tsx`: `EndpointFields`/`EndpointField`, `AxisValuesField`, `GridPreview`, and form parsers near `parametersFromForm`. Eval and grid share API schemas but not a single generator component.
- Gallery filter state is local to `GalleryScreen`; it is initialized from query/local storage. `galleryHash` serializes filters/page/pageSize/asset/return. `safeReturnTarget` rejects absolute/hash/scheme-looking values, but the origin is an opaque relative string rather than a typed route context.
- `AsyncAction`, `RatingControl`, `GroupComment`, notes/review controls are duplicated or local rather than one shared card/action system.

## API endpoint map

All routes below are under `/api` unless shown otherwise. `integrations.py` declares `/api/...` itself and is included without a prefix. Hidden compatibility/mutation routes are real even when excluded from OpenAPI.

| Area | Endpoints |
| --- | --- |
| System | `GET /health` and `/api/health`; `GET /system/version`, `/system/capabilities`, `/system/enums`, `/system/doctor` |
| Workspace/profile/project | `GET /workspaces`; `GET/POST /profiles`; `GET /profiles/active`; `GET/PATCH /profiles/{id}`; `PUT /profiles/{id}/active`; `POST /profiles/{id}/archive`; `GET/POST /projects`; `GET/PATCH /projects/{id}` |
| Assets/gallery | `GET/POST /assets`; `GET/DELETE /assets/{id}`; `GET /assets/{id}/locations|metadata|context|content|thumbnail`; `GET /gallery`; `GET /gallery/context` |
| Typed delivery | `POST /assets/{revision}/delivery`; `/delivery/alternate`; `/delivery/diagnose`; `GET /asset-deliveries/{capability}` |
| Datasets | `GET/POST /datasets`; `GET /datasets/{id}` (includes explicit linked training runs/empty reason); `GET /datasets/{id}/versions`; `GET /dataset-versions/{id}` and `/items` (includes origin/availability); `POST /datasets/{id}/drafts`; `GET/DELETE /dataset-drafts/{id}`; `PATCH .../items/{id}`; `POST .../operations[/preview]`; `POST .../publish` |
| Training/model | `GET/POST /runs`; `GET /runs/{id}` and `/metrics|config|checkpoints|samples`; `POST /runs/{id}/refresh|checkpoints|samples`; `GET /checkpoints/{id}`; `POST /checkpoints/{id}/hydrate`; `GET/POST /models`; `GET /models/{id}`; `GET/POST /model-versions`; `GET /model-versions/{id}`; `POST .../approve|archive`; `PATCH .../state` |
| Collaboration/jobs | `GET/POST /reviews|comments|notes|lineage`; `GET /activity`; `GET/POST /jobs`; `GET /jobs/{id}` and `/logs`; `POST /jobs/{id}/cancel` |
| Prompt/eval/grid | `GET/POST /prompt-sets`; `GET/PUT /prompt-sets/{id}`; `POST .../versions`; `POST /prompt-sets/generate`; `GET/POST /eval-definitions`; `GET /eval-definitions/{id}`; `GET/POST /eval-runs`; `POST /eval-runs/preview`; `GET /eval-runs/{id}` and `/outputs`; `POST .../cancel`; `GET/POST /grid-definitions`; `GET /grid-definitions/{id}`; hidden `POST .../runs`; compatibility `GET/POST /grids`, `GET /grids/{id}`, hidden `POST /grids/{id}/run[s]`, `GET /grid-runs/{id}/cells` |
| FAL | `GET /eval-endpoints`; `GET /eval-endpoints/{id}/schema`; `GET /providers/fal/endpoints`; `GET /providers/fal/test`; `POST /providers/fal/endpoints/{id}/validate`; `POST /fal-admissions`; `GET /fal-admissions/{id}`; `POST .../admit` |
| Imports/connections | `GET/POST /import-sources`; `PATCH/DELETE /import-sources/{id}`; `POST .../test|detect`; `GET .../browse`; `GET /connections`; `POST /connections/{provider}/test`; `POST /import-jobs`; `GET /import-jobs/{id}`; `POST .../retry|cancel`; `POST /local-imports` |
| Operator/transfers | aliases `GET/PATCH /operator-settings` and `/settings/operator`; aliases `POST .../test/{provider}`; `GET/POST /transfers`; `GET /transfers/{id}` and `/download`; `POST .../cancel`; `GET /dashboard`; `GET /search`; `GET /config` |
| Storage | `GET/PATCH /storage-policy`; migration router mounted by `storage_policy.py`; delivery/admission routes above |

Router ownership: `core.py` (workspace/project/assets/gallery), `datasets.py`, `runs.py`, `collaboration.py`, `operator.py`, `operator_config.py`, `integrations.py`, `compat.py`, `asset_delivery.py`, `fal_admissions.py`, and `storage_policy.py`. Storage migration endpoints are defined in `routers/storage_migrations.py` and included through the storage-policy router.

## Database tables and entity relationships

### Product-domain tables

- `workspaces` -> `user_profiles`, `projects`, `assets`, `import_sources`, `provider_jobs`, collaboration/job rows and storage policy rows.
- `projects` -> `datasets`, `training_runs`, `models`, `prompt_sets`, `eval_definitions`, `grid_definitions`; `assets.project_id` is nullable/`SET NULL`.
- `datasets` -> versioned immutable `dataset_versions` -> `dataset_items` -> `assets`. Editable `dataset_drafts` clone a base version into `dataset_draft_items`; `caption_operations` audit bulk edits.
- `training_runs` optionally reference a `dataset_version` and an origin `import_source`; they own `training_stages`, `training_metrics`, `checkpoints`, and `samples`.
- `checkpoints` reference a model-kind `asset`; storage identity/revisions are in `checkpoint_revisions`. A checkpoint can be registered once as a `model_version` (`model_versions.checkpoint_id` is unique). A `model` is a project-owned family/container.
- `samples` connect a training run, optional checkpoint/step, and image `asset`.
- `prompt_sets` own ordered `prompts`. `eval_definitions` connect project + endpoint + optional model version + prompt set + parameters. `eval_runs` snapshot a definition/checkpoint; `eval_outputs` connect the run/prompt to an image asset.
- `grid_definitions` connect an eval definition and JSON axes; `grid_cells` directly connect the definition to eval outputs. There is no persisted `grid_runs` table.
- `reviews`, `comments`, `notes` use string `subject_type`/`subject_id` polymorphism with no FK. `lineage_edges` is likewise generic. `jobs`/`activity_events` carry generic JSON/subject references.
- `import_sources`, `import_jobs`, `provider_jobs` track remote sources and work.

Full ORM table inventory: `workspaces`, `user_profiles`, `local_preferences`, `projects`, `assets`, `asset_locations`, `image_metadata`, `datasets`, `dataset_versions`, `dataset_items`, `dataset_drafts`, `dataset_draft_items`, `caption_operations`, `training_runs`, `training_stages`, `training_metrics`, `checkpoints`, `samples`, `models`, `model_versions`, `prompt_sets`, `prompts`, `eval_definitions`, `eval_runs`, `eval_outputs`, `grid_definitions`, `grid_cells`, `import_sources`, `import_jobs`, `provider_jobs`, `reviews`, `comments`, `notes`, `lineage_edges`, `jobs`, `activity_events`, `content_blobs`, `local_roots`, `source_capabilities`, `storage_policies`, `storage_policy_snapshots`, `storage_migrations`, `storage_migration_previews`, `storage_migration_preview_items`, `migration_execution_owners`, `storage_transfers`, `storage_reservations`, `publication_attempts`, `publication_receipts`, `storage_migration_items`, `checkpoint_revisions`, `fal_admissions`, `fal_subjects`, `fal_submission_intents`, `fal_artifact_receipts`.

Migration `0003_flexible_image_storage.py` additionally owns non-ORM operational tables: `cleanup_receipts`, `storage_leases`, `storage_imports`, `storage_import_items`, `storage_return_drafts`, `storage_feature_gates`, `storage_verification_receipts`, `storage_cutover_marker`, `raw_cutover_backups`, `sanitized_cutover_backups`, `sanitized_backup_bindings`, `cutover_attempts`, and `post_marker_restore_attempts`.

### Asset and provenance contract

`assets` is the logical file/image row. Durable bytes are separated into deduplicated `content_blobs` and one-or-more `asset_locations`. Locations retain provider, source, bucket/key, ETag/version/fingerprint, local root/path, sizes/hashes, `modified_at`, verification, and hydration state. `preferred_location_id` and `origin_location_id` pick roles. FAL assets also point to a `fal_artifact_receipt` and use `provenance_kind`.

Product membership must be read through explicit association tables (`dataset_items`, `samples`, `eval_outputs`, `checkpoints`) rather than guessed from `Asset.kind` or JSON metadata. `core.py::_asset_metadata` currently assembles a presentation projection by probing these relationships and may infer model attribution from LoRA filenames; that inference is not authoritative lineage.

## Image URL, S3, cache, and publication flow

1. S3 import stores an `Asset` plus remote `AssetLocation` (`source_id`, exact bucket/key, ETag/version/fingerprint, `modified_at`) and may create dataset/run/sample/checkpoint relationships. Imported remote originals need not be copied locally.
2. UI `AssetImage` posts a typed variant (`content` or thumbnail with `max_pixels`) to `/api/assets/{id}/delivery`; it never constructs an S3 URL.
3. `AssetDeliveryService` resolves the asset/revision and viable location. It can return an exact presigned remote URL, bounded verified proxy capability, or local/thumbnail capability. Delivery URLs are opaque, host-bound, expiring and process-local; a stale image error causes one descriptor refresh.
4. `/api/asset-deliveries/{capability}` serves proxy/local/cache bytes. Thumbnail cache is derivative only, capacity-controlled, and not the durable original.
5. Checkpoint hydration queues `checkpoint.hydrate`; the worker downloads/verifies/publishes atomically into configured local storage and records a checkpoint revision.
6. FAL admission resolves a checkpoint revision, snapshots storage policy/capability and obtains a provider-accessible subject URL. The worker submits the request, downloads outputs, publishes them locally, records `FalArtifactReceipt`, `Asset`, and `EvalOutput`. Remote writable output policy is not yet the default flow.

Legacy `GET /assets/{id}/content|thumbnail` exists, but runtime React uses typed delivery.

## Source types and credential validation

- Persisted import sources are currently only `provider="s3"`; “Mega S4” is an S3-compatible preset, not a separate domain provider. Endpoint may be AWS-default or custom; addressing style is `auto`, `path`, or `virtual`.
- Each source stores nonsecret configuration: endpoint, region, addressing style, bucket, allowed prefixes, credential environment prefix, managed prefix, active state, and identity fingerprint. Credentials are never persisted.
- API create/update validates source name/bucket length, credential prefix pattern, addressing style, normalizes configured prefix hints, and strips endpoint trailing slash. `allowed_prefixes` are optional browse hints, not an authorization boundary: `S3Browser.require_allowed` currently normalizes a requested key but deliberately does not reject access outside the hints.
- Credentials resolve from `<PREFIX>_ACCESS_KEY`, `<PREFIX>_SECRET_KEY`, optional `<PREFIX>_SESSION_TOKEN`; both access and secret must be present together. Default startup also understands `S3_*` and `MEGA_*`; README notes normal boto3/AWS chain for default S3, while the newer per-source credential helper is stricter. This divergence needs a deliberate compatibility decision.
- Source test performs a one-object browse against the first configured prefix hint, or the bucket root when there is no hint, and returns 502 on client/network/credential failure. Operator `storage`/`fal` tests are configuration-only; `/connections/fal/test` only verifies env presence and reports `network_tested: false`.
- `source_capabilities` cache immutable/read/write/delete/versioning/reachability evidence and credential epoch, supporting delivery and FAL admission decisions.

## FAL configuration and upload/request fields

Supported adapters are `ideogram/v4/lora` and `fal-ai/krea-2/turbo/lora` (`integrations/fal/adapters.py`, documented in `FAL_ENDPOINT_SCHEMAS.md`). Shared request fields are `prompt`, `loras[{path,scale}]` (max 3; scale 0-4), `image_size`, `num_images` (1-4; forced to 1 for grid cells), nullable integer `seed`, `sync_mode` (forced false), `enable_safety_checker`, and `output_format` (`jpeg|png`).

- Ideogram adds `expansion_model`, `rendering_speed`, and `acceleration`; custom dimensions are 512-3840 and multiples of 16. Default output is JPEG.
- Krea adds `acceleration` and `enable_prompt_expansion`; prompt max is 5000 and custom dimensions max 14142. Default output is PNG.
- DAM-owned normalized parameters add required `endpoint_id` and `lora_scale`. FAL admission requires project, model/checkpoint revision, prompts, normalized parameters, mode/kind, and a client request ID; admission is then explicitly acknowledged before provider work.
- Checkpoint upload is not a raw browser upload field. `loras[0].path` is derived from the admitted checkpoint revision/storage policy. `fal_url`/`fal_status` currently live in model-version `readiness` JSON; receipt/subject/intent tables are the stronger audit trail.
- Operator FAL settings persist default endpoint and maximum parallel jobs; API keys remain `FAL_KEY`/`FAL_API_KEY` environment values.

## Shared-file collision map for later packets

High collision risk:

- `apps/web/src/screens-assets.tsx`: DATASET, PROJECT, MODEL, GALLERY, SAMPLE/lightbox work.
- `apps/web/src/screens-evals.tsx`: GEN, EVAL, GRID, review/lightbox work.
- `apps/web/src/screens-core.tsx`: PROJECT, imports, settings/source work.
- `apps/web/src/App.tsx`, `shell.tsx`, `ui.tsx`, `styles.css`, `vela-theme.css`: routes, global navigation, shared selectors/cards/dropdowns/filters and UI packets.
- `apps/web/src/api.ts`, `hooks.ts`, `asset-image.tsx`: response shape, query serialization, fetch lifecycle, all image cards/lightboxes.
- `apps/api/titles_api/models.py`, `schemas.py`, `services.py`, Alembic revisions: every schema-bearing packet; serialize schema work before UI packets rely on it.
- `routers/core.py`: project/assets/gallery/context and metadata; `routers/runs.py`: model/run/checkpoint/sample; `routers/collaboration.py` + `compat.py`: eval/grid/prompt; `routers/integrations.py`: imports/FAL schemas.
- `apps/worker/titles_worker/import_jobs.py`, `fal_jobs.py`, `sqlalchemy_eval_sink.py`, `fal_request_resolver.py`: timestamp/provenance/admission changes.
- Tests most likely shared: `apps/api/tests/test_api_workflow.py`, `test_asset_read_dtos.py`, `test_asset_delivery.py`, `test_fal_admissions.py`, `test_fal_artifacts.py`; web tests and test setup are sparse.

## Known schema/API inconsistencies and packet blockers

1. **Timestamp contract resolved by DATA-07:** migration `0007_asset_origin_timestamps` adds `EvalOutput.generated_at` and `Sample.modified_at`, backfills historical rows deterministically, and gallery ordering uses the applicable canonical origin timestamp with ID tie-breaking. API rows expose `origin_type`, `generated_at`, and `modified_at` without substituting one timestamp for another.
2. **Revision identity mismatch:** most DTOs expose `asset_revision_id = Asset.id`. Typed delivery accepts an “asset revision” UUID, while true checkpoint revisions live in `checkpoint_revisions`. This naming can make asset, checkpoint, and revision IDs interchangeable in frontend code. Do not expand the alias without defining revision semantics.
3. **Origin preservation is partial:** gallery overlay preserves its own filters/page/pageSize and optional opaque `return`; standalone `#/image/:id`, eval, grid, samples, project image panels, and cross-page cards do not share a typed origin contract. `ImageReviewScreen` reconstructs only project/kind with `limit=250`, losing exact query/filter/page context.
4. **Entity integrity gaps:** `create_eval_definition` verifies existence but not that project, prompt set, and model version belong to the same project. `TrainingRun.dataset_version_id`, checkpoint stage/asset, sample checkpoint/asset, and some create paths likewise lack complete same-project/same-run validation. UI selectors filter, but API integrity cannot depend on UI.
5. **Grid execution model is missing:** no `grid_runs` table; `grid_cells` belong directly to a definition and jobs are found via JSON payload. Repeat runs, status/history, immutable axis/parameter snapshots, and cell provenance are ambiguous. Compatibility endpoints call a definition ID a grid-run ID.
6. **Metadata remains a lossy projection:** assets have one nullable `project_id`, while dataset/run/checkpoint/model/eval/source links are spread across explicit association tables. DATA-06 removed LoRA-filename model inference; model/checkpoint/run attribution is emitted only from explicit relationships. Assets reused across versions/contexts can still have multiple valid associations, so a single flattened card remains lossy.
7. **Model/checkpoint API shape:** model is only a family row; model version owns a unique checkpoint. `GET /model-versions/{id}` returns a raw row, unlike list/detail enrichments. `fal_status`/URL are untyped readiness JSON. Later MODEL packets need one stable artifact DTO.
8. **Eval route contract duplication:** canonical admission flow exists beside hidden `/eval-runs` mutations and `/eval-runs/preview`; grids have both definition and compatibility routes. OpenAPI omission does not remove runtime dependencies. Consolidation must preserve compatibility or migrate callers/tests.
9. **Workspace scope is implicit:** `current_workspace()` chooses the first workspace, sources/list operations generally scope to it, while project/run/model list endpoints often accept no workspace and rely on local single-workspace assumptions. UI displays first workspace and has no switcher.
10. **Source credential behavior diverges:** README/boto3 default chain and `S3Settings` optional credential pair differ from strict `SourceCredentials.from_environ`. Decide whether default AWS chain is supported per source before tightening validation.
11. **Pagination/response inconsistency:** some lists return arrays, gallery can return `{items,total,limit,offset}`, and `listOf()` guesses among many keys. Query names mix `project` (hash) with `project_id` (API), `pageSize` with `limit/offset`, and `status` aliases. Add typed DTOs rather than extending guessing.
12. **Asset deletion is logical-row deletion:** locations cascade and many association FKs restrict. Any bulk delete/UI work must distinguish database references, remote objects, local blobs/cache, and storage cleanup receipts; never imply S3 deletion.

## Tests and exact commands

Run from repository root unless noted.

```bash
# Install/sync
uv sync --extra dev
pnpm --dir apps/web install

# Entire Python suite (API, worker, CLI, integrations)
uv run pytest -q

# Focused backend areas
uv run pytest -q apps/api/tests
uv run pytest -q apps/worker/tests
uv run pytest -q tests/integrations

# Frontend unit tests, typecheck, contract audit, production build
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web audit:contracts
pnpm --dir apps/web build

# Migrations against configured TITLES_DATABASE_URL/default SQLite
uv run alembic current
uv run alembic heads
uv run alembic upgrade head
uv run alembic check

# Local manual runtime
uv run titles server start
uv run titles server status
pnpm --dir apps/web dev
uv run titles server stop
```

Migration tests live in `apps/api/tests/test_database_migrations.py` and storage migration/cutover tests. API workflow coverage is concentrated in `test_api_workflow.py`; typed delivery has dedicated delivery/thumbnail/storage tests; FAL has API admission/artifact/recovery plus worker grid/sink tests. Frontend currently has only `asset-image.test.tsx` and `run-loss-graph.test.tsx`; route/filter/lightbox preservation has no dedicated regression suite.
