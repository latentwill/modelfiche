# Modelfiche QA implementation ledger

Status legend: `completed`, `pending`, `blocked`. Initialized by MAP-01 on 2026-07-12.

## Task status

| Packet | Task IDs |
| --- | --- |
| Mapping | **MAP-01 — completed** |
| Data | **DATA-01 — completed; DATA-02 — completed; DATA-03 — completed; DATA-04 — completed; DATA-05 — blocked (one historical v002 source absent); DATA-06 — completed; DATA-07 — completed** |
| Project | **PROJECT-01 — completed; PROJECT-02 — completed; PROJECT-03 — completed; PROJECT-04 — completed; PROJECT-05 — completed; PROJECT-06 — completed; PROJECT-07 — completed** |
| Model | **MODEL-01 — completed; MODEL-02 — completed; MODEL-03 — completed; MODEL-04 — completed; MODEL-05 — completed; MODEL-06 — completed; MODEL-07 — completed; MODEL-08 — completed; MODEL-09 — completed** |
| Generator | GEN-01 — pending; GEN-02 — pending; GEN-03 — pending; GEN-04 — pending; GEN-05 — pending; GEN-06 — pending |
| Gallery | GALLERY-01 — pending; GALLERY-02 — pending; GALLERY-03 — pending; GALLERY-04 — pending; GALLERY-05 — pending; GALLERY-06 — pending; GALLERY-07 — pending; GALLERY-08 — pending; GALLERY-09 — pending; GALLERY-10 — pending |
| Eval | EVAL-01 — pending; EVAL-02 — pending; EVAL-03 — pending; EVAL-04 — pending; EVAL-05 — pending; EVAL-06 — pending; EVAL-07 — pending; EVAL-08 — pending; EVAL-09 — pending; EVAL-10 — pending; EVAL-11 — pending |
| Grid | GRID-01 — pending; GRID-02 — pending; GRID-03 — pending; GRID-04 — pending; GRID-05 — pending; GRID-06 — pending; GRID-07 — pending; GRID-08 — pending; GRID-09 — pending; GRID-10 — pending |
| UI | UI-01 — pending; UI-02 — pending |
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
- Full archive reconciliation was still active at the last ledger update: 59/61 objects and 12,498,864,961/14,339,372,681 bytes were size-verified in `s3://titlesxyz/title-lora/local-archive/TitlesXYZ/`. The uploader is resumable and non-overwriting.
