# Titles DAM Operator Implementation Plan

## 1. Objective

Build a desktop-first, local-first digital asset manager for image-model training that gives an operator one reliable place to:

1. Create and organize projects.
2. Browse datasets and completed training runs in MEGA S4.
3. Import their metadata without automatically downloading large artifacts.
4. Curate images and version captions.
5. Inspect training configuration, checkpoints, samples, and lineage.
6. Generate evaluations through FAL.
7. Compare outputs, rate them, and mark them as candidates, approved, held, or rejected.
8. Preserve enough ownership and provenance data to add teammates and client review later without redesigning the core model.

The first operational release is local and has no authentication. It supports multiple local user profiles so comments, notes, ratings, decisions, imports, and edits retain attribution. One profile is active at a time in the UI or CLI. Application state lives in local SQLite. Imported files and generated previews use a managed local asset/cache directory. MEGA S4 remains a first-class remote source and intake surface.

## 2. Evidence Reviewed

The plan is based on:

- `PRODUCT.md` and all 14 HTML wireframe surfaces.
- Desktop renders of every surface at 1440 x 1000.
- Mobile renders of the dashboard, dataset editor, Eval viewer, and Grid lightbox.
- Static design detection across all wireframes.
- A read-only inspection of the configured MEGA S4 bucket.

Observed remote structure:

```text
s3://latentwillbackup/title-lora/
  datasets/
    vcribb/
      ideogram-v4-captions-v002/
      qwen-captions-v002/
  projects/
    smoke/runs/...
    title/runs/...
```

The inspected `title-lora/projects/title/runs/` prefix currently contains approximately:

- 1,817 objects and 79.4 GB.
- 172 `.safetensors` files.
- 831 sample images.
- 549 logs.
- Multiple trainer formats, histories, optimizer states, manifests, run states, YAML/JSON configs, PID files, and SQLite sidecars.

Important integrity finding: top-level run manifests currently report empty checkpoint arrays even when the corresponding prefix contains up to 32 current `.safetensors` objects. The importer must treat manifests as useful metadata, not as an authoritative inventory.

## 3. Wireframe Evaluation

### Overall assessment

The wireframe has a sound product model and unusually complete task coverage. Its strongest idea is the separation between overview, editing, comparison, and full-screen inspection. It is ready to become a product specification, but it currently represents a mature application rather than a minimal first release.

The implementation should retain the object model while reducing the number of independent systems built before the primary workflow is usable.

### What should remain

- Workspace and project separation.
- Project-contained datasets, training runs, model artifacts, and Evals.
- Gallery as a query over every visual asset rather than another storage location.
- Dataset editing with selection-aware batch operations and caption versions.
- Separate comparison surfaces for prompt sets and training steps.
- Shared image lightbox and dedicated Grid lightbox.
- Visible model, dataset, checkpoint, prompt, seed, endpoint, and lineage metadata.
- Collapsible inspector that preserves workbench context.
- Explicit import/export area, implemented later when collaboration work begins.

### What should change before implementation

1. **Separate import from run detail.** `run-import.html` currently combines the concept of importing and inspecting a run. Use an S3 import browser and preview flow for discovery, then route imported runs to a normal run-detail page.
2. **Clarify Models.** `models.html` mixes model files, Evals, training runs, and sample comparisons. Rename this project section to **Models**, but model each checkpoint/model artifact and training run separately. The page can aggregate them without conflating them in storage.
3. **Keep the sidebar bounded.** Do not render every dataset, model, and Eval as an indefinitely growing navigation tree. Show stable project sections in navigation and a small recent/pinned subset. Full lists belong in the section workspace.
4. **Make project status actionable.** Dashboard graphs and activity summaries should be derived from real operations. Do not build ornamental metrics before the operations that generate them exist.
5. **Use explicit decision state.** A 1-5 rating measures quality; it does not record the production decision. Reviews should also support `candidate`, `approved`, `hold`, and `reject`.
6. **Make batch edits reversible.** Dataset find/replace and add-word operations need previews, transaction boundaries, and undo through immutable version history.
7. **Reduce inspector duplication.** Keep object metadata, notes, and recent activity in the inspector, but do not repeat information already visible in the main workspace.
8. **Constrain operator responsiveness.** Support laptop and desktop widths first. Phone layouts can provide read-only inspection later; dense caption and six-column comparison tools should not be compromised to claim phone support.

### Surface-by-surface disposition

| Surface | Evaluation | Delivery |
| --- | --- | --- |
| Dashboard | Useful project entry point; metrics depend on downstream data | Release 3 |
| Project | Strong context hub; should emphasize current datasets, runs, and decisions | Release 1 |
| Dataset | Highest-value operator surface; close to implementation-ready | Release 1 |
| Models | Valuable, but must separate runs from artifacts in the data model | Release 2 |
| Run Import / Detail | Essential, but must become two flows | Release 1 |
| Gallery | Useful as a derived asset query with virtualization | Release 1 |
| Image Lightbox | Shared primitive needed by Dataset, Gallery, Samples, and Evals | Release 1 |
| Sample Viewer | Core checkpoint-selection workflow | Release 2 |
| Eval Generator | Core integration; start with the endpoints actually used | Release 2 |
| Eval Viewer | Core review surface; add explicit production decision | Release 2 |
| Grid Generator | Valuable advanced evaluation tool | Release 3 |
| Grid Lightbox | Correctly specialized and should stay free of comments/sidebar | Release 3 |
| Transfers | Primarily supports collaboration/client handoff | Release 4 |
| Settings | Implement only connections and storage paths initially | Release 1, expand later |

### Visual design direction

The production UI should remain quiet and work-focused, but not inherit the wireframe's all-monospace greybox treatment.

- Use a neutral light work surface by default so caption editing, metadata, and logs remain comfortable for long sessions. Let actual images carry most color.
- Use one legible sans-serif family for interface text and a monospace face only for paths, hashes, IDs, seeds, steps, and configuration values.
- Use restrained semantic color for selection, running, complete, warning, failed, candidate, approved, and rejected states.
- Keep cards to real repeated objects. Workbench sections should be unframed bands, toolbars, tables, grids, or split panes.
- Standardize one application shell, button vocabulary, field style, density scale, focus treatment, and z-index scale.
- Use Lucide icons for familiar actions, with text where the command would otherwise be ambiguous.
- Preserve vertical page scrolling. Internal virtualized lists and image grids may scroll within stable panes when necessary.
- Support keyboard navigation for image movement, selection, rating keys `1`-`5`, decision actions, lightbox close, and previous/next.
- Remove wireframe redline annotations and numbered markers from the product.
- Replace padding/width transitions with transform/opacity or instant layout changes.

## 4. Lean Product Boundary

### First usable workflow

```text
Create project
  -> browse an allowed MEGA S4 prefix
  -> detect and preview a dataset or training run
  -> import remote metadata and cache image previews
  -> inspect/edit the dataset or inspect the run
  -> compare samples/checkpoints
  -> create a FAL Eval
  -> rate and mark candidate/approved/hold/reject
```

### Explicitly not required for the first operational release

- Authentication, invitations, and role enforcement. Named local profiles are included.
- Client review links or feedback package round-trips.
- PostgreSQL or a multi-node deployment.
- Full local mirroring of S3 training runs.
- Upload, delete, rename, or move operations in the S3 browser.
- Generic support for every trainer and FAL endpoint.
- Mobile operator editing.
- Rich dashboard analytics.
- Agent orchestration or training launch controls.
- A general-purpose DAM taxonomy engine.

These exclusions are sequencing decisions, not permanent product exclusions.

## 5. Recommended Architecture

### Repository structure

```text
titles-dam/
  apps/
    api/                 FastAPI application
    web/                 React + TypeScript application
    worker/              durable SQLite-backed background worker
    cli/                 agent-oriented and human-usable command line
  packages/
    contracts/           generated API types / shared schemas
    ui/                  design tokens and reusable UI components
  fixtures/
    s3/                  redacted object listings and small manifests
  docs/
    wireframes/          current HTML reference artifacts
    decisions/           short architecture decision records
  var/                   ignored local runtime data
```

### Backend

- Python 3.12.
- FastAPI and Pydantic for HTTP APIs and validated provider schemas.
- SQLAlchemy 2 and Alembic for persistence and migrations.
- SQLite in WAL mode for the first deployment.
- `boto3`/`botocore` for AWS S3 and S3-compatible access, including MEGA S4.
- Pillow plus optional `pyvips` for image metadata and thumbnail generation.
- A separate lightweight worker process that polls a durable `jobs` table in SQLite.
- Structured JSON logging with automatic credential and signed-URL redaction.

Avoid Redis and a distributed queue until more than one application host is required. Long-running imports and FAL polls still need durable job records; they should not run only inside web-request processes.

### Frontend

- React, TypeScript, and Vite.
- TanStack Router and TanStack Query.
- TanStack Table for dense metadata tables.
- Virtualized image grids/lists for large galleries and run samples.
- Radix primitives for accessible popovers, menus, dialogs, and tooltips.
- Lucide icons.
- CSS variables/tokens with either CSS Modules or a restrained utility layer. Avoid a page-specific copy of styles for each route.
- Vitest and Testing Library for UI behavior.
- Playwright for full operator workflows and viewport checks.

### Local runtime

```text
SQLite database:  var/titles-dam.sqlite3
Asset store:      var/assets/<sha256-prefix>/<sha256>
Thumbnail cache:  var/cache/thumbs/<asset-id>/<size>.webp
Temp downloads:   var/tmp/<job-id>/
```

Configuration stores the MEGA endpoint, bucket, and allowed prefixes. Credentials are supplied at process start from 1Password environment references and are never written to SQLite or browser storage.

Example runtime boundary:

```text
S3_BUCKET=<aws-or-compatible-bucket>
S3_ALLOWED_PREFIXES=<allowed-prefixes>
# S3_ENDPOINT is omitted for AWS and set for S3-compatible platforms.
# Legacy MEGA_* names remain supported for the existing Titles bucket.
```

## 6. Domain Model

Use UUIDs internally. Preserve provider IDs, names, paths, and hashes as external identifiers rather than using them as primary keys.

### Core ownership

- `workspace`: present now even though there is only one.
- `user_profile`: named local identity with display name, initials/avatar color, optional email, and active/archived state. No password or login is required.
- `local_preferences`: active profile and workstation-scoped UI/CLI preferences.
- `project`: free-form work scope with title, description, state, and trigger words.

### Assets and storage

- `asset`: logical image, model file, config, manifest, caption file, log, or other artifact.
- `asset_location`: local path or S3 URI, provider, bucket, key, ETag, size, modified time, hydration state, and last-seen time.
- `image_metadata`: dimensions, color mode, orientation, EXIF summary, and perceptual hash.
- `lineage_edge`: typed relationship between domain objects, such as `derived_from`, `trained_with`, `sampled_from`, or `evaluated_with`.

An asset may be remote-only, locally cached, or fully hydrated. A remote object must not be duplicated merely because it appears in more than one view.

### Datasets

- `dataset`: stable identity inside a project.
- `dataset_version`: immutable snapshot with source URI, caption format, trigger words, and status.
- `dataset_item`: asset, caption value, caption format, inclusion state, tags, and ordering within a version.
- `dataset_draft`: mutable working copy based on one immutable version.
- `caption_operation`: recorded batch operation with preview, scope, before/after counts, and operator.

Publishing a draft creates a new immutable dataset version. Undo restores or creates another draft; it never mutates a published version in place.

### Runs and model artifacts

- `training_run`: project, dataset version, trainer, base model, status, source prefix, raw manifest, raw state, and normalized configuration.
- `training_stage`: optional sub-run or named config within a multi-stage run.
- `checkpoint`: training run/stage, step, remote asset, state, and optional registered model-version association.
- `sample`: image asset, run, checkpoint/step, prompt, seed, and normalized generation metadata.
- `model`: stable named model identity.
- `model_version`: a checkpoint intentionally registered for continued use, with trigger words, base model, notes, lifecycle state `candidate|approved|archived`, and readiness states such as local/FAL.

### Evals and reviews

- `prompt_set` and `prompt`: versioned ordered Eval inputs.
- `eval_definition`: endpoint adapter, model version, prompt-set version, and generation parameters.
- `eval_run`: immutable execution instance with remote provider job IDs and status.
- `eval_output`: generated image plus prompt, seed, and provider metadata.
- `grid_definition`: two selected parameter axes and ordered values.
- `grid_cell`: references an Eval output rather than storing a rendered composite.
- `review`: subject, local user profile, rating 1-5, decision `candidate|approved|hold|reject`, and timestamps.
- `comment` and `note`: subject-scoped markdown/text with edit history.

### Operations

- `import_source`: provider configuration and allowed root prefix.
- `import_job`: selected prefix, detected type, state, progress, warnings, and results.
- `provider_job`: FAL submission and polling state.
- `activity_event`: append-only audit event generated by meaningful mutations.

Do not build a separate activity subsystem. Create events in the same transaction as the operation they describe.

## 7. S3 Browser and Importer

The connector supports standard AWS S3 with no custom endpoint and S3-compatible
platforms with an optional endpoint, region, and `auto|path|virtual` addressing.
Each source stores a credential environment prefix, never credentials. When its
explicit `<PREFIX>_ACCESS_KEY` and `<PREFIX>_SECRET_KEY` variables are absent,
boto3 uses the standard AWS credential chain. MEGA S4 remains supported through
the legacy `MEGA_*` environment names.

### Browser scope

Build a small read-only browser inside the Import surface, not a generic storage manager.

Required UI:

- Connection status and allowed source label.
- Breadcrumb navigation constrained to configured prefixes.
- Prefix rows followed by object rows.
- Name, detected type, size, modified time, and import state.
- Pagination/continuation support.
- Current-prefix filtering.
- Selection of a dataset or run prefix, not thousands of individual artifacts.
- Detection preview before import.
- Refresh and retry.
- No delete, rename, move, upload, ACL, or bucket administration actions.

### Detection

Recognize a prefix as a dataset when it contains strong signals such as:

- `captions.jsonl`.
- image and same-stem `.txt` pairs.
- `caption_audit.md`, `checksums.sha256`, or dataset config.

Recognize a prefix as a training run when it contains signals such as:

- `run_manifest.json` or `run_state.json`.
- trainer YAML/JSON configuration.
- `.safetensors` files.
- `samples/` images or sample filename step patterns.

The preview must show both declared and observed inventory:

```text
Manifest: 0 checkpoints
Observed: 32 current checkpoints, 272 current samples
Warning: manifest inventory is incomplete; observed objects will be indexed
```

### Import behavior

1. List and classify objects without downloading their bodies.
2. Exclude `_history/` by default while reporting that history exists.
3. Read only small metadata/configuration objects during preview.
4. Upsert objects idempotently by provider, bucket, key, and ETag.
5. Normalize known trainer configuration while preserving the raw source.
6. Register checkpoints as remote-only assets.
7. Fetch sample images only as needed to generate cached thumbnails.
8. Fetch dataset images progressively; create thumbnails and optionally hydrate originals according to the import choice.
9. Verify SHA-256 lists when the relevant object is hydrated.
10. Record warnings instead of failing the entire import for one malformed object.

### Default inclusion policy

| Object type | Index | Cache preview | Download original |
| --- | --- | --- | --- |
| Manifest/state/config | Yes | n/a | Yes, small text only |
| Dataset image | Yes | Yes | User-selectable, default yes for an actively edited dataset |
| Caption text/JSONL | Yes | n/a | Yes |
| Training sample | Yes | Yes, lazy | No |
| `.safetensors` checkpoint | Yes | n/a | No; explicit action only |
| Optimizer `.pt` | Metadata only | No | No |
| Logs | Metadata plus bounded tail on demand | No | No |
| SQLite/WAL/PID sidecars | Metadata only | No | No |
| `_history/` content | Count/index summary | No | No by default |

### Refresh and reconciliation

- Refresh compares ETag, size, and modified time.
- Missing remote objects become `remote_missing`; records are not silently deleted.
- Changed manifests are stored as new source snapshots.
- Completed imports can be rerun safely.
- Run status is normalized from state files but may be overridden by an explicit operator decision when stale backup states remain.

## 8. API Boundaries

Keep APIs resource-oriented and task-specific. Representative endpoints:

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}

GET    /api/import-sources
GET    /api/import-sources/{id}/browse?prefix=&cursor=
POST   /api/import-sources/{id}/detect
POST   /api/import-jobs
GET    /api/import-jobs/{id}
POST   /api/import-jobs/{id}/retry

GET    /api/datasets/{id}
POST   /api/datasets/{id}/drafts
POST   /api/dataset-drafts/{id}/operations/preview
POST   /api/dataset-drafts/{id}/operations
POST   /api/dataset-drafts/{id}/publish

GET    /api/runs/{id}
POST   /api/runs/{id}/refresh
GET    /api/runs/{id}/samples
POST   /api/checkpoints/{id}/hydrate
POST   /api/model-versions

POST   /api/eval-runs
GET    /api/eval-runs/{id}
POST   /api/eval-runs/{id}/cancel

POST   /api/reviews
POST   /api/comments
GET    /api/gallery
GET    /api/assets/{id}/thumbnail
GET    /api/assets/{id}/content
```

Generate the frontend API client/types from the OpenAPI document. Do not maintain duplicate hand-written request types.

The agentic CLI is a first-class API client and must not implement a second set of domain rules. Its complete contract is defined in `CLI_SPEC.md`.

## 9. Delivery Plan

### Release 0: Foundation

Deliverables:

- Move wireframes into `docs/wireframes/` without changing their content.
- Scaffold API, web, worker, migrations, and test harnesses.
- Add design tokens and the stable application shell.
- Add SQLite schema for workspace, local user profiles, project, asset, location, jobs, and events.
- Scaffold the CLI with profile selection, machine-readable output, health checks, and job inspection.
- Add local asset/cache services and secret-redacted logging.
- Capture redacted S3 listings and small manifests as deterministic fixtures.

Exit criteria:

- One command starts API, worker, and web development processes.
- Migrations create a clean local database.
- Restarting any process does not lose queued job state.
- No credential or signed URL appears in logs, API payloads, or browser storage.
- The same operation performed through UI or CLI produces the same domain result and activity event.

### Release 1: Import, Dataset, Gallery, and Run Inspection

Deliverables:

- Project creation and project workspace.
- Local profile creation, switching, and attribution in comments/activity.
- Minimal Settings for local paths and MEGA connection test.
- Read-only MEGA S4 browser constrained to allowed prefixes.
- Dataset and training-run detection preview.
- Durable, idempotent imports with progress and warnings.
- Dataset version/draft model and caption editor.
- Selection-aware caption batch operation preview and publish flow.
- Training run detail with raw config, normalized summary, checkpoint inventory, and sample groups by step.
- Gallery derived from indexed assets.
- Shared image lightbox with metadata and previous/next navigation.
- Local thumbnail generation and remote-preview cache.

Exit criteria:

- Import `title-lora/datasets/vcribb/qwen-captions-v002/` into a project and display all 54 image/caption pairs.
- Edit captions, preview a batch operation, publish a new immutable version, and reopen it after restart.
- Import `kzapata-specialist-track-b-v002/` without downloading its checkpoint files.
- Reconcile the empty manifest checkpoint list with the observed 32 current checkpoints.
- Browse cached samples while the application remains responsive.
- Rerunning either import creates no duplicate assets, runs, or dataset versions.
- Complete the same import and dataset publication workflow through non-interactive CLI commands.

This is the first genuinely usable operator release.

### Release 2: Model Decisions and FAL Evals

Deliverables:

- Create named model versions from selected checkpoints without moving or modifying the source artifact.
- Training-step Sample viewer with bounded comparison columns.
- Ratings plus `candidate|approved|hold|reject` decisions.
- Versioned prompt sets.
- Endpoint adapter registry with schema-driven fields.
- Implement the verified Ideogram 4 LoRA and Krea 2 Turbo LoRA mappings in `FAL_ENDPOINT_SCHEMAS.md` as the first two adapters.
- FAL model upload/readiness state where required.
- One fully supported Eval endpoint first, then the second observed endpoint.
- Durable FAL submission, polling, retry, cancellation, and output ingestion.
- Eval viewer for prompt-set comparisons.
- Notes/comments and transaction-backed activity events.

Exit criteria:

- Select checkpoints from an imported run and compare the same sample prompts across steps.
- Register a selected checkpoint as a named candidate model version, then approve it independently.
- Generate an Eval from a saved prompt set, survive an API/worker restart, and ingest outputs.
- Rate outputs and record a production decision.
- Trace every Eval output back to endpoint parameters, prompt, seed, model version, checkpoint, run, and dataset version.

### Release 3: Grids, Dashboard, and Operator Polish

Deliverables:

- Two-axis Grid definitions using the endpoint adapter's valid parameters.
- Grid execution composed from individual Eval outputs.
- Zoomable/scanable Grid lightbox.
- Dashboard project summaries derived from real data.
- Bounded recent activity and pinned/recent navigation.
- Saved filters, stronger keyboard workflow, skeletons, empty states, errors, and recovery actions.
- Performance pass for large galleries and run histories.
- Laptop-width adaptation and accessibility audit.

Exit criteria:

- Generate and reopen a saved grid without creating a giant composite image.
- Navigate and inspect a grid entirely by keyboard.
- Dashboard counts reconcile with underlying records.
- A 10,000-image indexed gallery remains scrollable through virtualization.
- No horizontal page scrolling at supported desktop/laptop widths.

This completes the operator-focused product represented by the wireframes.

### Release 4: Team and Client Handoff

This is intentionally separated from the current operator build.

- Move production persistence to PostgreSQL when concurrent writers are introduced.
- Add authentication, workspace membership, and roles.
- Map existing local user profiles to authenticated accounts rather than replacing their attribution history.
- Keep full teammates on the same operator routes with authorization checks.
- Create a responsive client review route in the same web application, exposing curated Evals only. Client handoff becomes a URL rather than an exported package by default.
- Add review assignments, approval milestones, notifications, and optional export/import packages.
- Decide whether remote assets remain in MEGA S4, move to a product-managed bucket, or use both through the existing storage adapter.

## 10. Testing Strategy

### Importer

- Unit tests for dataset and run detection from redacted key listings.
- Parser tests for AI Toolkit YAML/JSON and observed alternate trainer formats.
- Reconciliation tests for empty/stale manifests versus observed objects.
- Idempotency tests using ETag changes, missing objects, duplicate filenames, and partial failures.
- Botocore `Stubber` tests for pagination, transient errors, access denial, and malformed responses.
- Optional MinIO integration suite for S3-compatible behavior; do not depend on the live bucket for routine tests.

### Domain behavior

- Immutable dataset publication and draft branching.
- Batch-operation preview matches committed changes.
- Model-version registration, approval, and review state transitions.
- Lineage completeness for imported and generated assets.
- Activity events commit atomically with their operations.

### UI

- Component tests for selection scope, rating, decision, filters, and error recovery.
- Playwright coverage for the Release 1 and Release 2 golden paths.
- Screenshot checks at 1440 x 1000, 1280 x 800, and a narrow laptop/tablet boundary.
- Keyboard-only navigation and focus-order tests.
- Canvas/image pixel checks where thumbnail or Grid rendering is involved.

### Security and durability

- Secret-redaction tests.
- Path traversal and unsafe archive extraction tests.
- MIME sniffing and image decode failure tests.
- Presigned URL expiry behavior.
- Worker crash/restart and interrupted-download cleanup.
- Database backup/restore smoke test.

## 11. Operational Guardrails

- S3 browser is read-only in the operator releases.
- No automatic deletion of remote or local originals.
- Downloads go to a job-scoped temporary file and move atomically after checksum/size validation.
- Enforce cache and free-space limits before downloading large artifacts.
- Do not proxy entire remote checkpoints through the browser.
- Paginate S3 listings and database queries.
- Generate multiple thumbnail sizes once; do not repeatedly decode original images for every view.
- Preserve raw manifests/configs alongside normalized fields.
- Store timestamps in UTC and display the operator's local timezone.
- Treat provider paths and filenames as untrusted input.
- Back up SQLite before destructive migrations and expose a simple local backup command.

## 12. Decisions to Record During Implementation

Create short ADRs when each is resolved:

1. Asset identity and content hashing for remote-only objects.
2. Dataset draft/version publication semantics.
3. First supported FAL endpoint and model-upload lifecycle.
4. Run/stage normalization for multi-stage AI Toolkit prefixes.
5. Local cache eviction and explicit artifact pinning.
6. Transition trigger from SQLite to PostgreSQL.
7. Client review boundary and authorization model.
8. CLI/API compatibility and schema versioning policy.

## 13. Immediate Build Order

Start with one thin end-to-end slice rather than building all foundational services horizontally:

1. Scaffold API/web/worker and create the database.
2. Configure the MEGA connection through environment variables supplied by 1Password.
3. Implement prefix browsing and run/dataset detection against fixtures, then validate read-only against the live bucket.
4. Create a project and import the `vcribb/qwen-captions-v002` dataset.
5. Render real images/captions in the Dataset surface and publish a caption revision.
6. Import `kzapata-specialist-track-b-v002` as remote metadata and render its run/checkpoint/sample detail.
7. Add Gallery and the shared image lightbox using those same indexed assets.
8. Stabilize Release 1 before beginning FAL execution or Grid generation.

This order produces a usable tool early while exercising the architecture against the actual data shapes and scale.
