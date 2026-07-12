# Modelfiche QA implementation ledger

Status legend: `completed`, `pending`, `blocked`. Initialized by MAP-01 on 2026-07-12.

## Task status

| Packet | Task IDs |
| --- | --- |
| Mapping | **MAP-01 — completed** |
| Data | **DATA-01 — completed; DATA-02 — completed; DATA-03 — completed; DATA-04 — completed; DATA-05 — blocked (one historical v002 source absent); DATA-06 — completed; DATA-07 — completed** |
| Project | **PROJECT-01 — completed; PROJECT-02 — completed; PROJECT-03 — completed; PROJECT-04 — completed; PROJECT-05 — completed; PROJECT-06 — completed; PROJECT-07 — completed; PROJECT-08 (`QA2-01`) — completed** |
| Model | **MODEL-01 — completed; MODEL-02 — completed; MODEL-03 — completed; MODEL-04 — completed; MODEL-05 — completed; MODEL-06 — completed; MODEL-07 — completed; MODEL-08 — completed; MODEL-09 — completed** |
| Shell | **SHELL-01 (`QA2-03`) — completed; SHELL-02 (`QA2-04`) — completed; SHELL-03 (`QA2-12`) — completed** |
| Generator | **GEN-01 — completed; GEN-02 — completed; GEN-03 — completed; GEN-04 — completed; GEN-05 (`QA2-02`) — completed; GEN-06 — completed**; GEN-07 (`QA2-05`) — pending |
| Gallery | GALLERY-01 — pending; GALLERY-02 — pending; GALLERY-03 — pending; GALLERY-04 — pending; GALLERY-05 — pending; GALLERY-06 — pending; GALLERY-07 — pending; GALLERY-08 — pending; GALLERY-09 — pending; GALLERY-10 — pending |
| Eval | EVAL-01 — pending; EVAL-02 — pending; EVAL-03 — pending; EVAL-04 — pending; EVAL-05 — pending; EVAL-06 — pending; EVAL-07 — pending; EVAL-08 — pending; EVAL-09 — pending; EVAL-10 — pending; EVAL-11 — pending; EVAL-12 (`QA2-06`) — pending; EVAL-13 (`QA2-07B`) — pending |
| Grid | GRID-01 — pending; GRID-02 — pending; GRID-03 — pending; GRID-04 — pending; GRID-05 — pending; GRID-06 — pending; GRID-07 — pending; GRID-08 — pending; GRID-09 (`QA2-07A`) — pending; GRID-10 — pending; GRID-11 (`QA2-08`) — pending; GRID-12 (`QA2-09`) — pending; GRID-13 (`QA2-10`) — pending |
| UI | UI-01 — pending; UI-02 — pending; UI-03 (`QA2-11`) — pending |
| Regression | QA regression — pending |

## MAP-01 completion record

- Status: completed.
- Files changed: `docs/qa/modelfiche-system-map.md`, `docs/qa/modelfiche-ledger.md`.
- APIs/schema/migrations changed: none.
- Components changed: none.
- Verification: repository route/component/API/ORM/migration/source/storage/FAL/test inventory; all requested task IDs found exactly in the status table; `git diff --check` passed; focused Python migration/asset DTO/FAL admission/eval-sink suite passed (10 tests). Frontend test/typecheck could not start because this worktree had no installed packages and restricted DNS prevented pnpm from fetching them. Contract audit could not connect to the intentionally unstarted API at `127.0.0.1:8400`.
- Blockers found for later packets: explicit EVAL `generated_at` and SAMPLE `modified_at` sort contracts are absent; asset/revision ID naming is ambiguous; origin/lightbox state preservation is incomplete; grid executions have no durable run entity; cross-project relationship validation and stable typed artifact DTOs are incomplete. See “Known schema/API inconsistencies and packet blockers” in the system map.

## DATA completion record

- Status: DATA-01, DATA-02, DATA-03, DATA-04, DATA-06, and DATA-07 completed. DATA-05 application discovery, association, image-only detection, hydration, and filtering are fixed and verified against real data. One historical version remains blocked: `kzapata-krea2-captions-v002` has no matching source objects in either configured bucket or `/Users/edkennedy/Kinamix/TitlesXYZ`; it remains at zero items rather than being mislabeled with v004 captions.
- Schema/migration: `0007_asset_origin_timestamps` adds nullable/indexed `samples.modified_at` and `eval_outputs.generated_at`; SQLite upgrade backfills SAMPLE from associated location `modified_at`, and EVAL in authoritative order from `response._history.ended_at`, `sent_at`, provider `generated_at`, then legacy `created_at`. Runtime ingestion uses the same provider ordering and restores UTC tzinfo on SQLite reads. Storage baseline head recognition advances to 0007 while accepting 0006.
- APIs: dataset detail exposes explicit training-run relationships and empty reason; dataset items expose `DATASET` origin and missing-location state; gallery/sample rows expose canonical origin/timestamps and order on them; source deletion is persistent only for unreferenced configurations and returns 409 with reference categories; MEGA connection tests select only MEGA-configured sources; new bounded `POST /api/local-imports` imports browser folders, captions/metadata, duplicates, and partial failures.
- Components: `ImportScreen` adds folder selection, progress, completion/partial failure/duplicate status; `DatasetScreen` shows linked runs, explicit empty state, and per-item missing-object errors; settings adds confirmed safe source deletion.
- Files: `models.py`, `app.py`, `storage/baselines.py`, `routers/{core,datasets,integrations,runs,local_imports}.py`, S3 import and FAL eval sinks, migration 0007, `screens-core.tsx`, `screens-assets.tsx`, migration/baseline fixtures and DATA regression tests, this map and ledger.
- Verification: combined API/worker suite 136 passed; independent focused review passed 11 backend/migration tests, then 10 DATA/detection tests after the image-only fix; frontend typecheck passed; frontend 6 tests passed; production build passed with the existing large-chunk warning; Python compileall, fresh SQLite `alembic upgrade head`/`current`, and `git diff --check` passed. Repository-wide `alembic check` still reports the pre-existing broad static-storage/ORM drift documented by MAP-01; it does not report a DATA-07 head/upgrade failure.
- Compatibility/risks: legacy EVAL rows lacking provider timestamps retain their historical `EvalOutput.created_at` interpretation during migration; local folder upload is intentionally bounded and synchronous (1000 files, 25 MiB/file, 250 MiB total) and removes newly written files when later database work fails; deleting referenced source configuration is rejected rather than cascading or touching remote S3 objects.

### DATA-05 real-storage reconciliation

- Source inventoried: `/Users/edkennedy/Kinamix/TitlesXYZ` (597 files, 19 GB).
- S3 destination: `s3://titlesxyz/title-lora/local-archive/TitlesXYZ/`; writes were additive and size-verified, with no overwrite or deletion.
- Uploaded/materialized: 285 objects, 1,033,018,031 bytes. This includes 54 VCRIBB images copied server-side from the archive bucket rather than re-uploaded.
- Successfully imported and hydrated: `kzapata-gemini-style-captions-v004` (30 images/30 captions), `flat` (16/16), `render` (14/14), `kzapata-krea2-nocaptions` (30/0), and `vcribb-ideogram4-json-captions-v003` (54/54). All import jobs completed without warnings; all 144 imported dataset images have local hydrated locations and S3 origins.
- Gallery filter verification: each populated dataset returns canonical `DATASET` rows with captions where available. Dataset filters also include derived `SAMPLE` rows from training runs that explicitly reference the dataset; this is relationship-driven, not filename inference.
- Remaining local material absent from both configured S3 buckets and not uploaded in the dataset-only execution: 61 files / 14,339,372,681 bytes. Groups are Finals (5), FAL run records (2), DiffSynth checkpoints (6), Krea v001 checkpoints (3), merged/overtrained/coverage candidates (27), specialist/VCRIBB checkpoint metadata or artifacts (12), one training zip, two runpod manifests, two VCRIBB contact sheets, and one VCRIBB config. Uploading these is a separate checkpoint/model archive operation, not dataset hydration.

## PROJECT and MODEL completion record

- PROJECT-01 through PROJECT-07 completed: relationship-backed previews, deterministic latest run, comparison removal, title-bar editing, visible create action, real breadcrumbs, and immediate workspace rename synchronization.
- MODEL-01 through MODEL-09 completed: contained previews, explicit checkpoint/run relationships, default loss graph below samples, stored training configuration with scientific learning-rate notation, actual checkpoint registry metadata, explicit FAL state/URL, secure FAL key save/clear/status/validation, and persisted candidate lifecycle controls.
- FAL configuration is write-only on disk (`0700` secret directory, `0600` files), resolves `FAL_KEY` then `FAL_API_KEY` then saved key, and was network-validated without generation. Migration `0008_checkpoint_revisions` backfilled operational revision identity.
- Live regression exposed and fixed 0007-to-0008 baseline recognition, immutable checkpoint revision mapping, storage-policy/snapshot/local-root ORM mappings, submission-intent admission linkage, provider lifecycle alias handling, internal parameter leakage, result fetching, and request-time provenance.
- Modern Times merged model registered as model `c4a1f73e-2f39-43cf-ae1e-2e762a9bfd03`, version `42a41764-3e59-4885-8747-b2c2f9c21c52`, with verified SHA-256 `1faaa99084fad957216e0cb74091d135ff7ef0f854c155fe8848b7c241974e5e` and its existing FAL URL.
- Exactly one authorized FAL request was submitted: `019f55bd-b4e0-7440-be18-fb58c7d6e30a`. Eval run `da2af79d-759e-4b4b-90bd-911ef3e2edc9` succeeded and ingested eval output `c5b3c0d3-9cf4-447c-a089-858d481a631b` / asset `f1c1f99c-780a-45e2-bc91-e4845dd63df7`, seed `249459924`, with canonical generated time `2026-07-12T09:52:03.808Z`.
- Verification: independent focused FAL/migration tests passed; agent backend/worker suite passed 145 tests (one unrelated gallery-contract test excluded); frontend typecheck passed; live server health and FAL auth validation passed; final worker job and eval run both report `succeeded`.
- Full archive reconciliation completed: all 61 remaining objects and 14,339,372,681 bytes were uploaded additively and size-verified in `s3://titlesxyz/title-lora/local-archive/TitlesXYZ/`. Final-object HEAD evidence: `kzapata-specialist-track-b-v002-coverage-merges/kzapata_full3x_v004-2400_x_render-v002-3000_x_flat-v002-3250_tf35-40-25_tr20-50-30-r160.safetensors`, 1,148,778,424 bytes, multipart ETag `8e764f9bba21cbc1a95e380b8b8b7f24-137`, modified `2026-07-12T10:01:07Z`.

## QA2 canonical alias mapping

QA2 requirements are implemented only through these canonical ledger tasks; there is no separate QA2 packet:

| QA2 alias | Canonical task | Recorded definition |
| --- | --- | --- |
| `QA2-01` | `PROJECT-08` | Project cards: Dataset left, Training Samples middle; focused completed-area delta. |
| `QA2-02` | `GEN-05` | Shared scoped + Image, Eval, and Grid actions on Project and Model pages. |
| `QA2-03` | `SHELL-01` | Dock Settings and Storage at the sidebar viewport bottom. |
| `QA2-04` | `SHELL-02` | Reachable, scrolling, unclipped sidebars coordinated with SHELL-01. |
| `QA2-05` | `GEN-07` | One persistent, ordered Image/Eval/Grid generation queue consuming normalized generator contracts. |
| `QA2-06` | `EVAL-12` | Add/edit/remove/reorder/review manual Eval prompts with preserved order. |
| `QA2-07A` | `GRID-09` | Existing task: remove Grid Prompt Source. |
| `QA2-07B` | `EVAL-13` | Make manual Eval prompts first-class; retain optional Prompt Source only if necessary and clear. |
| `QA2-08` | `GRID-11` | Direct fixed Grid prompt with conflict validation against Prompt axes. |
| `QA2-09` | `GRID-12` | Stable X-column/Y-row preview and result ordering, including Z-model sub-grids. |
| `QA2-10` | `GRID-13` | Validating, duplicate-safe primary Grid Generate action targeting GEN-07. |
| `QA2-11` | `UI-03` | Accessible, coherent metadata and semantic status capsules. |
| `QA2-12` | `SHELL-03` | Human-readable activity labels with raw keys only in technical details. |

## GEN-01–06 completion record

- Added normalized workflow, context, model/checkpoint, parameter, provider status/result, canonical generated-asset metadata, and future queue-envelope contracts shared by Image, Eval, and Grid.
- Added a reusable single/multi `ModelCheckpointPicker`; selections are constrained by Project or Model scope and unavailable registrations explain their missing revision/provider/endpoint evidence.
- FAL is isolated behind a provider adapter that derives its model URL and endpoint from `ModelVersion.readiness`, translates only supported normalized parameters, compiles the exact request, normalizes status/result, and routes through the existing admission/worker/ingestion path. Future providers such as ComfyUI extend the provider adapter/compile boundary without changing workflow definitions.
- Added prompt-set-optional persistence in migration `0009_inline_generation_prompts`: Eval definitions may use deterministic ordered inline prompts or a legacy PromptSet. Direct Image requests create no PromptSet rows. Worker planning prefers inline prompts, retains legacy compatibility, and tracks retry completion by stable logical inline prompt ID.
- Added scoped `+ Image`, `Eval`, and `Grid` actions to Project and Model pages. Added `#/generate` with preserved project/model/version context and eligible-image prefill; opening or prefilling never submits automatically.
- Added provider-neutral `POST /api/generation-requests/compile` with registration-derived provider/endpoint validation, cross-project/revision/model guards, exact inline-prompt persistence, identical replay idempotency, and conflicting replay rejection. Explicit form submission remains required before FAL admission.
- Verification: independent 22-test backend/migration/worker run passed; independent 9-test frontend generator/project run passed; frontend TypeScript project build passed. Agent broader focused runs passed 26 API/migration/baseline tests, 3 worker regressions, and 11 frontend tests. No live FAL admission or generation was submitted.
- Preserved pending regression: `test_gallery_context_uses_full_filtered_order_not_visible_page` remains under later Gallery triage and was not changed in this packet.

## PROJECT-08 and SHELL-01–03 completion record

- `PROJECT-08` (`QA2-01`): Project image cards now render Dataset images on the left, Training samples in the middle, and Eval outputs on the right while preserving their existing resources, loading/error/empty states, gallery links, and responsive single-column layout.
- `SHELL-01` (`QA2-03`) and `SHELL-02` (`QA2-04`): the navigation rail uses a dynamic viewport-height flex layout; its primary navigation is the independent scroll region while Settings and Storage remain in a fixed footer region. The inspector and mobile navigation remain independently scrollable and reachable at narrow viewport heights.
- `SHELL-03` (`QA2-12`): activity keys are mapped case-insensitively to human labels in the inspector and Project activity table. Unknown keys receive deterministic sentence-style labels; raw machine keys are confined to expandable technical details.
- Verification: independent full frontend suite passed 21 tests across 6 files; frontend TypeScript project build passed; `git show --check` and clean isolated-worktree verification passed.
