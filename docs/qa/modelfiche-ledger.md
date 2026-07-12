# Modelfiche QA implementation ledger

Status legend: `completed`, `pending`, `blocked`. Initialized by MAP-01 on 2026-07-12.

## Task status

| Packet | Task IDs |
| --- | --- |
| Mapping | **MAP-01 — completed** |
| Data | **DATA-01 — completed; DATA-02 — completed; DATA-03 — completed; DATA-04 — completed; DATA-05 — blocked (source objects absent); DATA-06 — completed; DATA-07 — completed** |
| Project | PROJECT-01 — pending; PROJECT-02 — pending; PROJECT-03 — pending; PROJECT-04 — pending; PROJECT-05 — pending; PROJECT-06 — pending; PROJECT-07 — pending |
| Model | MODEL-01 — pending; MODEL-02 — pending; MODEL-03 — pending; MODEL-04 — pending; MODEL-05 — pending; MODEL-06 — pending; MODEL-07 — pending; MODEL-08 — pending; MODEL-09 — pending |
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

- Status: DATA-01, DATA-02, DATA-03, DATA-04, DATA-06, and DATA-07 completed. DATA-05 application discovery/association/filter regressions are covered, but real Modern Times/Kzapata population is blocked because the inspected source roots contain no corresponding dataset image/caption objects; its four known dataset versions therefore correctly remain at zero items.
- Schema/migration: `0007_asset_origin_timestamps` adds nullable/indexed `samples.modified_at` and `eval_outputs.generated_at`; SQLite upgrade backfills SAMPLE from associated location `modified_at`, and EVAL in authoritative order from `response._history.ended_at`, `sent_at`, provider `generated_at`, then legacy `created_at`. Runtime ingestion uses the same provider ordering and restores UTC tzinfo on SQLite reads. Storage baseline head recognition advances to 0007 while accepting 0006.
- APIs: dataset detail exposes explicit training-run relationships and empty reason; dataset items expose `DATASET` origin and missing-location state; gallery/sample rows expose canonical origin/timestamps and order on them; source deletion is persistent only for unreferenced configurations and returns 409 with reference categories; MEGA connection tests select only MEGA-configured sources; new bounded `POST /api/local-imports` imports browser folders, captions/metadata, duplicates, and partial failures.
- Components: `ImportScreen` adds folder selection, progress, completion/partial failure/duplicate status; `DatasetScreen` shows linked runs, explicit empty state, and per-item missing-object errors; settings adds confirmed safe source deletion.
- Files: `models.py`, `app.py`, `storage/baselines.py`, `routers/{core,datasets,integrations,runs,local_imports}.py`, S3 import and FAL eval sinks, migration 0007, `screens-core.tsx`, `screens-assets.tsx`, migration/baseline fixtures and DATA regression tests, this map and ledger.
- Verification: combined API/worker suite 135 passed; DATA-focused suite 5 passed; frontend typecheck passed; frontend 6 tests passed; production build passed with the existing large-chunk warning; Python compileall, fresh SQLite `alembic upgrade head`/`current`, and `git diff --check` passed. Repository-wide `alembic check` still reports the pre-existing broad static-storage/ORM drift documented by MAP-01; it does not report a DATA-07 head/upgrade failure.
- Compatibility/risks: legacy EVAL rows lacking provider timestamps retain their historical `EvalOutput.created_at` interpretation during migration; local folder upload is intentionally bounded and synchronous (1000 files, 25 MiB/file, 250 MiB total) and removes newly written files when later database work fails; deleting referenced source configuration is rejected rather than cascading or touching remote S3 objects.
