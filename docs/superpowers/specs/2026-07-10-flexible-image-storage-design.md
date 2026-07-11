# Flexible Image Storage Design

**Date:** 2026-07-10  
**Status:** User-approved direction; design-review revision 11

## Problem

Titles DAM catalogs remote MEGA S4 objects without downloading large files, but image content and thumbnails currently require hydrated local files. Generated FAL images are also local-only. Operators need one simple choice between storing new image originals on this computer or in S3-compatible storage. Titles must preserve byte-level provenance, expose the actual source and copy state, recover safely, and never delete a verified prior copy during migration.

## Product Outcome

Routine Settings presents one decision:

```text
Store new images in:
  Mega S4 — available everywhere, uses less disk
  This computer — works offline
```

The current release remains a single-workspace desktop/operator application. The setting is installation-global for that workspace. App-managed S3 keys include the workspace UUID so later multi-workspace support does not require a key migration.

## Goals

- Let operators choose Local or S3 for new image originals.
- Automatically choose S3 only when one configured source is unambiguously the sole candidate and has passed the exact write probe.
- Freeze source identity and storage policy before a job is accepted.
- Apply the policy to imports and FAL outputs.
- Keep import choices simple: keep images at their origin or copy them to this computer.
- Distinguish immutable content revision, import origin, generated provenance, write destination, exact preferred physical location, and request fallback.
- Serve bound remote images without retaining durable local originals.
- Copy existing images on request without deleting verified locations.
- Organize app-managed S3 objects by workspace and project inside one configured bucket.
- Make setup, stale-source recovery, migration progress/cancellation/continuation, image failure, and source repair actionable and accessible.

## Non-Goals

- Moving SQLite application state to S3.
- Automatically deleting verified local or S3 copies.
- Moving objects after workspace/project renames.
- Creating/deleting buckets or changing public-access policy.
- Replacing explicit checkpoint hydration for filesystem-only tools.
- S3-backed export retention or CDN provisioning.
- A new general-purpose upload UI. Future uploads must consume this contract.
- Supporting multipart managed writes in this release.
- Supporting unauthenticated privileged APIs on LAN/public listeners, through reverse proxies/port forwards, or on shared multi-user hosts.

## Trust and Product Boundaries

- TCP loopback is a host-wide trust boundary, not an OS-user identity boundary. Every local process/user that can reach loopback can call the API.
- The supported no-auth configuration is a trusted single-user workstation. Unix-socket mode may provide owner-only CLI/headless access, but browser TCP remains host-wide.
- Exact Host and Origin checks protect browser flows from DNS rebinding/cross-site mutation; they do not defend against local malware.
- A future shared-host/LAN/remote deployment requires authentication and is outside this release.

## Terminology

- **Storage policy:** Installation setting used to resolve future image-producing jobs.
- **Content revision:** Immutable identity of one exact byte sequence or, before first byte verification, one exact source-scoped object revision.
- **Preferred location:** Exact `AssetLocation` used first for an asset revision.
- **Physical location:** One durable verified copy represented by an `AssetLocation`.
- **Origin location:** S3 object revision from which an imported asset revision was cataloged. Generated assets have no import origin.
- **Write destination:** Writable S3 source selected for generated images and explicit migrations.
- **Writable source:** Active S3 source whose immutable identity and managed prefix have a current successful capability result.
- **Fallback:** One alternate physical location for the same content revision used when preferred delivery fails.
- **Managed prefix:** Reserved prefix for Titles writes; default `titles-dam/managed/`.
- **Unbound legacy record:** Existing S3 location/run/checkpoint provenance whose owning source cannot be proven.

# Persistence and Invariants

## `LocalRoot`

Persist immutable roots: UUID/workspace/kind, canonical server path, stable fingerprint `(device, inode, owner UID, mode)`, created/retired/availability. Paths are never exposed. Settings changes create a new root; old locations never reinterpret.

All access begins from a retained root directory FD: relative-path validation, `openat`/platform equivalent with no-follow, regular-file/owner/mode/link-count checks, and pre/post identity recheck. Hard links and path traversal fail closed.

Durable publication protocol:

1. Write bounded chunks to a random attempt file through retained directory FDs; check live lease/fence/reservation before each ≤8 MiB chunk.
2. Flush/fsync complete content; verify size/SHA.
3. No-replace publish to the attempt final name; fsync every newly created directory and the final containing directory.
4. Re-open through retained FDs, revalidate identity/regular/link count, hash, and only then persist a publication receipt/DB location.
5. Recovery treats unreceipted final files as attempt-owned candidates only under the still-current fence; stale-fence files quarantine.

Each active Local writer holds an installation-supervisor OS lock plus DB lease. Lease expiry alone never releases byte reservation. Reclaimer first proves old process/FDs gone, closes/unlinks owned temp state, measures usage, then atomically releases reservation. ENOSPC or stale fence never commits a location.

Root swap, intermediate/final symlink, hard link, replacement, and crash ordering tests fail closed. Legacy cache originals may backfill only after full proof; derivatives/transients never become locations.
## `ContentBlob`, `Asset`, and immutable observations

`ContentBlob` is byte identity: UUID, unique SHA-256, verified size/MIME. It exists only after hashing.

`Asset` is immutable provenance and uses a tagged observation:

- imported: immutable source ID/key plus VersionId or ETag/size/time observation;
- generated: non-null `generated_artifact_receipt_id`;
- legacy: immutable `legacy_asset_id` baseline marker.

Add nullable one-time `content_blob_id`, exact `preferred_location_id`, optional imported `origin_location_id`, and `provenance_kind`. SHA verification attaches/reuses a blob; it never replaces observation or merges/repoints historical foreign keys.

New remote observation creates new Asset/location. Integrity mismatch marks location failed. Fallback/copy requires same blob or an exact-observation transfer that verifies/attaches that blob. Migration changes preferred location only after commit; origin does not change. Public `POST /api/assets` and caller-supplied `AssetLocationCreate` are removed; v1 image producers are admitted import and FAL paths only. Tests seed through repository fixtures. Any future upload feature must introduce its own gated snapshot/transfer/publication admission; no direct physical-location writer remains outside frozen legacy conversion and the sole repository commit.
## `AssetLocation`

Persist:

- workspace and asset IDs;
- provider (`local` or `s3`);
- S3 source ID, key, VersionId, ETag, observed size/time, source revision fingerprint;
- Local root ID and canonical relative path;
- verified SHA-256, size, state, last verification;
- repair attribution for legacy bindings.

Constraints:

- S3 rows have source/key/revision fields and no Local root/path;
- Local rows have immutable root ID/path and no S3 source/key;
- source-bound location workspace equals asset/source workspace;
- `(workspace_id, source_id, object_key, source_revision_fingerprint)` is unique;
- preferred/origin location belongs to the same asset/workspace;
- only `available` and digest/revision-compatible locations can be preferred or fallback.

Use composite foreign keys/unique keys where SQLite permits and same-transaction repository assertions for same-asset constraints. All storage paths use a workspace-scoped repository; bare ID or bucket/name/order lookups are prohibited.

## Other remotely actionable provenance and revision propagation

Every record that can initiate remote I/O binds a source, location, or immutable revision:

- `TrainingRun.origin_source_id` plus canonical source fingerprint; refresh uses only it.
- Import jobs/snapshots bind origin source and fingerprint.
- A logical `Checkpoint` remains the `(run, stage, step)` projection and gains `current_revision_id`.
- Immutable `CheckpointRevision` rows contain checkpoint ID, ordinal, asset ID, content blob/SHA when verified, captured source observation/location, created time, and superseded relation.
- `ModelVersion.checkpoint_revision_id` and `EvalRun.checkpoint_revision_id` pin exact immutable revisions. The FAL parent snapshot also pins that revision and handoff identity.
- Dataset `source_uri`, manifests, and display metadata are non-authoritative strings and never select credentials.

Revision propagation is deterministic:

| Aggregate/reference | Remote object revision change |
|---|---|
| Published `DatasetVersion` / `DatasetItem` | Published rows remain immutable. Reconciliation creates the next `DatasetVersion`, copies unchanged items, substitutes new asset revisions, and records predecessor/version lineage. |
| `DatasetDraftItem` | Remains pinned to its base/selected asset. Operator explicitly rebases or starts a draft from the new published version. |
| `TrainingRun` / `TrainingStage` | Logical rows retain source binding and mutable run status/config; artifact byte revisions never mutate their historical children. |
| `Checkpoint` | Same run/stage/step creates a new `CheckpointRevision` and atomically advances `current_revision_id` for future work. Old revisions remain. |
| `ModelVersion` | Remains pinned to its original checkpoint revision; advancing requires explicit operator action/new model version. |
| `EvalRun` and generated lineage | Remain pinned to admitted checkpoint revision and snapshot. Retry uses that revision/handoff; if unavailable, retry is blocked and a new run must be admitted against current revision. |
| `Sample` | New remote revision creates a new Sample row with `supersedes_sample_id`; latest projection changes, historical row remains. |
| `EvalOutput` | Immutable; a rerun creates a new EvalRun/output. |
| Reviews/comments/decisions | Stay on original subject/asset and never follow a current pointer. |

Idempotence keys include source revision fingerprint or checkpoint revision ID, so repeated reconciliation creates no duplicate version/revision rows.

Legacy TrainingRun prefixes bind only when evidence identifies one same-workspace source; otherwise refresh is disabled and explicit `Repair run source` verifies source/prefix. Legacy checkpoints/models bind only to a concrete repaired asset revision.
## Legacy FAL readiness values

`ModelVersion.readiness.fal_path` and generic HTTP/S3 readiness URLs are retired as authority:

- the schema migration removes raw `fal_path`/URL values from persisted readiness JSON;
- migration stores only a redacted `legacy_checkpoint_access_unavailable` reason when no concrete bound checkpoint location exists;
- the API/UI never returns/renders the retired URL;
- future readiness uses typed booleans/status plus concrete checkpoint location resolution;
- legacy models without a usable location show `Checkpoint source repair required` and cannot enqueue FAL work.

No raw signed/provider URL can be stored as readiness or job recovery state.

## `StoragePolicy`

Persist provider, S3 write source, selection origin (`automatic` or `operator`), `fal_local_fallback`, and attribution.

`fal_local_fallback` defaults to `true`. Under the S3 choice, Settings shows `If Mega S4 becomes unavailable after generation: Save the result to This computer` with an Advanced option `Fail without a local copy`. The value is snapshotted at FAL admission. An S3 source that is not Write ready blocks before any billable provider submission. After admission, a destination outage uses the captured choice. The global desired S3 policy remains unchanged, so the next newly admitted generation retries S3.

### Timing-independent automatic selection

Automatic selection uses **configured candidates**, not probe completion order:

1. A candidate is an active source configured for this workspace, regardless of pending/failed/ready probe state.
2. In one locked policy-reconciliation transaction, auto-pin S3 only when exactly one configured candidate exists and is Write ready.
3. Zero candidates, more than one configured candidate, or one pending/failed candidate yields automatic Local.
4. Probe completion re-counts candidates. If A and B were both saved before either probe finishes, either completion order leaves Local.
5. Once automatic S3 is pinned, later additions never change it.
6. Operator Local/S3 is never changed by discovery.
7. An ineligible pinned source remains desired; only operations that write there are blocked.

Settings explicitly renders: no candidates; sole pending/failed candidate; sole automatic Write-ready candidate; or multiple candidates requiring choice. A sole candidate becoming ready announces the automatic S3 choice and offers `Keep This computer`.
## `SourceCapability` and credential epoch

Capabilities are feature-scoped and bound to source fingerprint, adapter/implementation version, process boot ID, and current nonpersisted credential epoch:

- basic list/read;
- trustworthy conditional read;
- trustworthy versioned read;
- managed single-Put/create-only;
- exact-version/atomic-conditional cleanup;
- provider-specific FAL handoff.

All positive capabilities expire after 15 minutes; credential tuple change/expiry or restart invalidates immediately. Descriptor issuance, Keep-at-origin admission, and FAL admission require a current result; stale results re-probe, spool/hash, or block as stated below.

Probe contracts:

1. Conditional: write A, capture ETag, prove If-Match A; overwrite B; old If-Match must fail before body.
2. Versioned: A/V1 then B/V2; exact Head/Get V1 returns A; clean exact versions.
3. Managed write: conditional Put collision, exact read-back/service SHA, private policy, delete only under cleanup rule.
4. Cleanup: exact verified VersionId or equivalent atomic conditional delete; then prove that exact version disappeared and any replacement remains.
5. FAL handoff: current FAL adapters expose no non-billable provider-side fetch preflight. Therefore `locally_verified_handoff` proves a fresh exact-VersionId GET-only URL by fetching and hashing through SafeHttpTransport, validates public HTTPS/TTL, and binds adapter/source/credential epoch. It does **not** claim FAL egress reachability. If a future adapter exposes provider preflight, it records stronger `provider_verified_handoff`.

Only current versioned-read permits direct presign; only current conditional-read permits direct conditional stream. Keep-at-origin requires either. Basic-read-only may Copy locally. FAL admission accepts locally_verified or provider_verified handoff, but the former must disclose and acknowledge that FAL checkpoint fetch can fail after billing. Redacted failures persist; secrets do not.
## `StoragePolicySnapshot`

Before enqueue, persist a typed/versioned snapshot row referenced by the parent job/migration and all children:

- snapshot/storage-contract version;
- desired/resolved provider and captured `fal_local_fallback`;
- origin source ID and normalized immutable fingerprint for every S3 read;
- write source ID/fingerprint/managed prefix for every S3 write, including separate checkpoint staging source when needed;
- Local destination root ID;
- logical operation ID and actor/time.

An accepted snapshot freezes referenced source identity. Worker compares the fingerprint before I/O and fails blocked on drift.

S3 credentials use an isolated source factory. Exact `<PREFIX>_ACCESS_KEY` and `<PREFIX>_SECRET_KEY` are mandatory; `<PREFIX>_SESSION_TOKEN` is optional. Missing named credentials fail before DNS/network use. Every client receives those explicit values and disables shared profiles/config, generic AWS environment fallback, web identity, container/ECS, and instance-metadata providers. No ambient credential chain or unrelated `AWS_*` key is allowed. Credentials are not snapshotted.
## `StorageTransfer`, publication attempt, and receipt

Before I/O persist transfer identity/source/snapshot/destination plus an immutable `PublicationAttempt`: new UUID, attempt generation, random nonce, **publication fence**, deterministic final key/path containing attempt UUID, expected size/SHA, and state.

The publication fence is immutable ownership evidence; the active commit-lease fence is separate and mutable only by lease reclaim. S3 metadata stores attempt UUID/generation/nonce/publication fence/source digest. Local path is under the attempt UUID in the owner-only root.

Rules:

1. One claim/fence creates one attempt/key. A retry or reclaimed lease always creates a new attempt UUID/key/fence.
2. A receipt may commit only when `publication_fence == current active commit-lease fence`.
3. Crash after physical publication but before receipt under an expired/reclaimed fence does **not** promote that old target. It becomes `orphaned_old_fence`/quarantined; successor republishes at a new key.
4. A late stale worker publishing after expiry can therefore never be accepted by the successor.
5. Under the still-current fence, recovery probes the planned target. Matching metadata/path plus full SHA/source/snapshot creates `PublicationReceipt`; missing retries; mismatch/foreign quarantines and is never deleted/reused.
6. Cleanup deletes only a proven owned, unreferenced attempt target after its fence is terminal **and** the destination has current exact-version/atomic-conditional cleanup capability; otherwise quarantine is retained. Verified locations are never targets.

Receipt stores exact provider key/path, VersionId/ETag, size, SHA evidence, publication/commit fence and time. Source/destination verification remains exact VersionId/conditional read/descriptor-relative Local and single-Put/read-back.

The sole `BEGIN IMMEDIATE` commit transaction requires current fence, verified receipt and SHA; creates/reuses blob/location; sets transfer/attempt committed; attaches blob/preference; updates item; sets `post_marker_effect_epoch` nonzero for any Local/S3 publication; releases reservation; records redacted activity. Job completion follows.
## `StorageMigration` and `StorageMigrationItem`

### Scope and concurrent admission

V1 scope choices:

- `All image assets in this workspace` (default): `Asset.workspace_id = current workspace AND Asset.kind = image`;
- named project: same predicate plus direct `Asset.project_id = selected project`;
- `Unassigned images`: same predicate plus direct `Asset.project_id IS NULL`.

Indirect references from datasets/runs/evals never change scope ownership; a directly unassigned asset remains Unassigned even if referenced by a project-owned record. Freeze results in `ORDER BY Asset.created_at ASC, Asset.id ASC`.

Preflight persists ordered item rows and shows scope, eligible count, known/unknown bytes, exclusions. Eligible items have an exact compatible source and preferred location different from destination. Over 50,000 items or 100 GiB known bytes disables enqueue with project/new-only guidance.

Each item freezes ordinal, asset/content identity, selected source location/revision, destination, eligibility, and latest outcome. Preflight selects preferred exact location or deterministic verified same-blob alternate. Drift yields `source_changed`; only a new `Review changed items` preflight may select a new revision.

A partial unique active-parent constraint on workspace covers queued/running/cancel-requested. Saving the new policy and inserting the parent happen in one transaction. Concurrent second submission does **not** mutate policy; it returns HTTP 409 with existing parent UUID, state, and canonical detail route. UI links to that parent, including while canceling.

### Cancellation and continuation

Cancellation linearizes at the atomic transfer/location/preference/item/reservation transaction defined above.

| State when cancel is observed | Outcome |
|---|---|
| not started / pending | `canceled`, no I/O, Resume remaining |
| streaming / uploaded_unverified | stop at fence/chunk boundary; cleanup/quarantine owned bytes; `canceled`, Resume remaining |
| verified, transaction not committed | finish atomic commit; `copied`/`reused`, not remaining |
| committed | keep completed, not remaining |
| failed / source_changed | retain reason; Retry failed or Review changed as applicable |

Restart applies the same table. UI says a verified in-flight item may finish.

- Resume remaining: canceled/unstarted only.
- Retry failed: failures except source_changed; each operator action grants a new generation with up to three automatic retries.
- Continue incomplete: remaining plus retryable failed.
- Review changed items: new parent/preflight.
- Counts use latest terminal outcome.
## `Job` contract and server readiness gates

Every Job has `storage_contract_version`, optional snapshot ID, claim generation, and terminal reason. Add terminal `superseded` and `quarantined`; neither is reclaimable/generic-retryable. Upgrade jobs show Review and restart; mismatch jobs show redacted blocked detail and require their dedicated producer.

`StorageFeatureGate(name,state,version,verification_receipt_id,blocked_reason)` uses closed states disabled|migrating|ready|blocked. Only the packaged launcher control plane changes `storage_contract`; only repository-owned verifiers change dependent gates. Allowed transitions are disabled|blocked→migrating→ready|blocked. Ready with the same current receipt is idempotent. Evidence/run-lease mismatch affects zero rows and returns blocked without changing a ready gate.

With old processes reaped and exclusive installation lock/run lease held, launcher runs `titles storage verify-contract`. It verifies exact Alembic head, marker, sanitized binding/purge receipt, schema/contract checksums, journal absence, SQLite integrity, and no mixed-version process. One BEGIN IMMEDIATE transaction rechecks run lease/evidence, records immutable `StorageContractVerificationReceipt`, and changes `storage_contract` migrating→ready. Failure records only a redacted blocked reason. Any later binding/head/contract mismatch blocks storage startup and invalidates dependent-gate receipts.

Every dependent readiness receipt references the current storage-contract receipt/version; transition is forbidden otherwise. After sanitized restore, `storage_contract` verification is mandatory first. Worker validates head/marker/storage-contract receipt, feature gate/snapshot and claim fence before recovery/dispatch.

`GET /api/system/capabilities` exposes `storage_contract`, `source_bound_io`, `remote_delivery`, `policy_import`, `policy_fal`, and `storage_migration`, each with state/version/redacted reason/receipt version.

| Producer route/action | Gate and snapshot rule |
|---|---|
| import queue and Transfers shortcut | `policy_import`; fresh origin/mode snapshot |
| import retry | `policy_import`; return to preview, never clone payload |
| TrainingRun refresh | `policy_import`; fresh bound run source/fingerprint |
| checkpoint hydrate | `source_bound_io`; exact checkpoint/source/root snapshot |
| eval/grid/FAL retry | `policy_fal`; admitted run pins revision/policy |
| migration enqueue/resume/retry | `storage_migration`; frozen parent/item |
| S3 descriptor/alternate | `remote_delivery`; Local remains available |

Every route first requires `storage_contract=ready` with current receipt and then its named gate. Failure returns 409 `storage_feature_not_ready` with gate/state/redacted reason and creates no row. Generic `/api/jobs` accepts only non-protected kinds. UI never transitions gates.
## SQLite serialization and compare-and-swap protocol

SQLite uses WAL plus short explicit `BEGIN IMMEDIATE` critical transactions; external I/O never occurs inside them. A storage repository helper retries `SQLITE_BUSY` at 25/100/250 ms, then returns stable 409 `storage_busy_retry` without partial mutation.

Schema/CAS rules:

- one unique `StoragePolicy.workspace_id` row;
- partial unique active migration index on workspace;
- partial unique nonterminal transfer index on logical operation/subject/destination;
- Job/lease/transfer/migration rows have monotonic generation/version columns.

Policy reconciliation and policy-plus-migration admission run under `BEGIN IMMEDIATE`, recompute after locking, commit once. Lease acquire/reclaim CASes owner/state/expiry/generation; heartbeat/release requires owner+generation+active. `StorageTransfer` additionally captures nullable migration parent/owner epoch. Migration claim, each ≤8 MiB chunk, publication/recovery/receipt, and final commit require both transfer lease/generation and current `MigrationExecutionOwner(parent_id,owner_epoch)`. Final commit starts BEGIN IMMEDIATE and aborts stale on any zero-row fence; it never publishes location/preference/item state.
# Source Identity and Compatibility Cutover

## Immutable referenced sources and canonical fingerprint

Endpoint, region, addressing style, bucket, credential-environment prefix, allowed prefixes, and managed prefix become immutable once any snapshot/location/transfer/run references a source. Display name and active state remain editable. Secrets rotate behind the same environment names. Identity changes create a replacement source. Environment bootstrap reuses only an identity-equal row; drift creates replacement.

Fingerprint format is `source-v1:<hex SHA-256 of canonical UTF-8 JSON>`. Canonical JSON has sorted keys and no insignificant whitespace:

- endpoint: lowercase scheme, IDNA/lowercase host, explicit normalized port, no default-port duplication, no trailing slash/path/query/fragment/userinfo; AWS default is the literal `aws-default`;
- bucket: exact case-sensitive value;
- region and addressing style: lowercase normalized values;
- credential prefix: uppercase validated identifier;
- allowed prefixes: path-normalized, segment-safe, trailing slash normalized, deduplicated, sorted;
- managed prefix: same normalization, non-empty, and segment-boundary-contained by one allowed prefix.

Fingerprint comparison is constant-time after format/version validation. Any normalization failure rejects the source before persistence/network use.
## Legacy binding and baseline conversion

Cutover preserves each legacy Asset ID and every existing foreign key as the baseline `legacy` observation; it never guesses historical bytes or repoints DatasetItem/DraftItem, Checkpoint, Sample, EvalOutput, metadata, reviews/comments, lineage, or export references.

| Legacy location set | Baseline disposition |
|---|---|
| valid root-safe Local | root-bind location; make preferred; attach ContentBlob only after actual hash verification, never from claimed hash alone |
| valid cache-only Local | bind legacy cache root/preferred; mark legacy-durable; future writes use asset root |
| exact uniquely source-bindable S3 | create immutable current observation location; compatibility unverified until bytes hash |
| ambiguous S3 | unbound/unavailable; explicit repair |
| Local plus S3 with no proven common SHA | keep both on baseline but only valid Local preferred; S3 cannot fallback; later hash mismatch creates a new S3 Asset revision while all old FKs remain baseline |
| multiple locations with verified equal SHA | attach/reuse one ContentBlob and mark compatible |
| conflicting verified SHA | baseline retains preferred historical Local if valid; conflicting location becomes a new Asset revision and is never silently substituted |
| no usable location | baseline unavailable; FKs remain and UI shows reimport/restore requirement |

For each legacy logical Checkpoint, create ordinal-1 `CheckpointRevision` pinned to its existing asset and make it current; existing ModelVersion/EvalRun pins that baseline revision. Sample/EvalOutput rows remain immutable. Unique `legacy_asset_id`/`legacy_checkpoint_id` markers make conversion idempotent. Verification queries assert all pre-cutover foreign-key counts/IDs remain.
## Immutable migration baselines

Replace mutable `0001_initial` with committed frozen legacy DDL; `0002_generic_s3_sources` also remains immutable. New migrations never import live head metadata. Supported starting states are classified **after verified raw backup and before mutation** by exact table/column/index/FK/default/check-constraint fingerprint:

| State | Action |
|---|---|
| empty database | run frozen base→0001→0002→head |
| exact unversioned current `create_all` legacy schema | atomically adopt/stamp 0002, then upgrade |
| exact stamped 0001 | upgrade through 0002→head |
| exact stamped 0002 | upgrade to head |
| exact head plus marker 1 | normal validated startup |
| unknown/drifted/partial or version-shape mismatch | leave unmodified; offline restore/diagnostic only |

Commit fixtures/checksums for empty, unversioned legacy, stamped 0001, and stamped 0002. Temporary-copy harness upgrades each without current `Base.metadata.create_all`; fresh-head create_all remains isolated model-test-only.
## Cutover ownership, database-set backup, and interruption recovery

Packaged launcher stops/reaps old processes and holds the exclusive installation lock/run lease. Before any SQLite open it checks external restore/cutover journals; unfinished database-set recovery completes or blocks.

Raw and sanitized backups use SQLite online backup plus random backup/key IDs, private plaintext temp, streaming AES-256-GCM chunks/manifest, fsync, independent decrypt/hash/integrity-check, atomic finalization and keychain reconciliation. `RawCutoverBackup` binds DB/workspace/target marker. Its ≤24-hour clock starts only after marker commit or successful raw restore/abandon receipt; armed marker-absent attempts retain it indefinitely.

After schema/conversion/scrub/job disposition/FK/count/lineage validation, create `SanitizedCutoverBackup` from exact head with all gates disabled, marker absent and `post_marker_effect_epoch=0`. Authenticated header binds raw ID/hash, head, scrub manifest, target marker and validator set. Launcher atomically inserts marker 1, binding receipt, and effect epoch 0. Sanitized envelope retains seven days for pre-enable rollback/diagnosis.

External 0600 `CutoverAttempt` records backup identities, fingerprints/actions, target and phases. Marker-present restart validates/finalizes. Armed marker-absent never resumes; offline raw restore is required.

All raw/sanitized restore uses one `DatabaseSetRestoreJournal` and handles main, `-wal`, and `-shm` as a set. Under exclusive lock it checkpoints/closes the prior DB, fsyncs it, journals, renames prior main and any sidecars into an ID-bound quarantine directory, fsyncs, installs the checkpointed restored main with no sidecars, proves canonical sidecars absent, and fsyncs the installation directory. Startup sees the journal before SQLite and deterministically finishes from recorded hashes/phases. Crash before old-set quarantine keeps old authoritative; after quarantine resumes install; after main install never pairs it with old sidecars. Quarantine remains through restored startup validation.

`titles storage recover-cutover --raw` verifies/decrypts the raw envelope to private temp, runs the set replacement, records raw restore receipt, removes the cutover journal, and begins a new cutover. It never synthesizes marker state.

`titles storage recover-cutover --sanitized <id> --confirm "ROLL BACK TO CUTOVER SNAPSHOT"` is permitted only while current DB has exact head/marker/binding, every gate including `storage_contract` disabled, effect epoch 0, and no post-marker intent/transfer/publication rows. Otherwise it returns `restore_after_enable_forbidden`, preserves current DB set and envelope, and creates no restore journal. Every effect-capable producer sets epoch nonzero in its admission transaction before external I/O.

Allowed sanitized restore verifies envelope/header/hash/integrity/head/manifest/validator set; requires restored marker absent/gates disabled/effect epoch 0; then on temp DB one exclusive transaction inserts marker/binding, unique restore receipt/generation, revokes leases and keeps all gates disabled. It checkpoints/closes and uses database-set replacement above. Retry is idempotent by exact generation/header/marker/receipt; mismatch preserves both sets and blocks. Envelope/key/quarantine remain until startup validates the restored set. First re-enable step is `titles storage verify-contract`.

Crash injection covers every journal write, checkpoint/close, old main/WAL/SHM quarantine, decrypt, marker transaction, main install, sidecar-absence check, directory fsync, validation and purge. Attempts after accepted FAL or published transfer are rejected without mutation.
## Nonterminal legacy job disposition

Before marker `1`, **every** nonterminal job with missing/version-0 contract is fail-safely terminalized, regardless of kind or payload:

- known import/hydration/FAL kinds and arbitrary/unknown/malformed generic kinds become `superseded/storage_contract_upgrade`;
- payload/result/error/activity are structurally redacted and raw lifecycle/artifact URLs scrubbed in the same transaction;
- no legacy running job is requeued;
- API list/detail treats superseded as terminal and shows only redacted reason plus dedicated Review/restart action when classifiable;
- unclassifiable jobs show no retry action.

After cutover, any protected job with wrong contract/snapshot/fence becomes terminal `quarantined/storage_contract_mismatch`. Tests include arbitrary generic queued jobs and running protected jobs.
# Physical Selection and Serving

## Deterministic preferred/fallback selection

Preferred exact location is first. One alternate may be selected only if content revision/SHA matches:

1. verified same-provider copy, newest verification first;
2. verified other-provider copy when automatic fallback is allowed;
3. unavailable.

Creation time, bucket, name, and source ordering are never authority. Migration makes requested verified destination preferred. Request fallback never changes it.

## Delivery variants, descriptor, diagnosis, and client state machine

`Asset` is the immutable image revision; changed source bytes create a new Asset. `asset_revision_id` is therefore exactly `Asset.id`, not a second entity or mutable version. The canonical image route is `#/image/<asset_revision_id>`. Dataset item, caption, gallery, model-eval, EvalOutput, GridCell and Sample read DTOs all expose this field; caller migration may rename legacy `asset_id` but never synthesizes it client-side.

```text
AssetDeliveryVariantV1 =
  | {kind:"content"}
  | {kind:"thumbnail", max_pixels:256|512|1024}
AssetDeliveryRequestV1 = {
  asset_revision_id:UUID, variant:AssetDeliveryVariantV1
}
AssetDeliveryDescriptorV1 = {
  version:int,
  diagnostic_token:string,
  asset_revision_id:UUID,
  selected_location_id:UUID,
  provider:"local"|"s3",
  source_label:string,
  variant:AssetDeliveryVariantV1,
  delivered_mime_type:string,
  delivered_size_bytes:int|null,
  mode:"direct_s3"|"proxy",
  delivery_url:string,
  expires_at:string,
  fallback_used:boolean,
  alternate_token:string|null,
  summary_id:string
}
AlternateDeliveryRequestV1 = {
  alternate_token:string,
  diagnostic_token:string,
  asset_revision_id:UUID,
  variant:AssetDeliveryVariantV1
}
DiagnoseDeliveryRequestV1 = {diagnostic_token:string}
DeliveryNextActionV1 =
  | {kind:"reacquire_descriptor_once"}
  | {kind:"consume_alternate_once", alternate_token:string}
  | {kind:"open_storage_details", href:string, focus_target_id:string}
  | {kind:"user_retry_once"}
  | {kind:"stop"}
DeliveryFailureV1 = {
  code:"capability_expired"|"primary_failed_alternate_available"|
       "storage_configuration_required"|"delivery_capacity"|
       "native_delivery_failed"|"alternate_failed",
  redacted_message:string,
  retryable:boolean,
  action:DeliveryNextActionV1
}
AssetDeliveryResultV1 =
  | {kind:"descriptor", descriptor:AssetDeliveryDescriptorV1}
  | {kind:"failure", failure:DeliveryFailureV1}
DiagnoseDeliveryResultV1 = {kind:"failure", failure:DeliveryFailureV1}
```

`POST /api/assets/{asset_revision_id}/delivery` takes `AssetDeliveryRequestV1`; path/body IDs must match. `POST .../delivery/alternate` takes `AlternateDeliveryRequestV1` JSON—token never query/header. `POST .../delivery/diagnose` takes `DiagnoseDeliveryRequestV1`. `GET /api/asset-deliveries/{capability}` returns bytes or typed failure. Delivery/alternate return descriptor or failure using 200/400/404/409/410/429/503; diagnose returns 200 `DiagnoseDeliveryResultV1`. Tokens bind Host, revision, variant, location, expiry and descriptor version. Typed URL/token fields never enter generic serializers.

Presign/proxy capability replays until five-minute expiry. Alternate token is one-use/60-second/process-boot. Proxy records terminal outcome. Direct-S3 diagnose revalidates server expiry, current capability and exact-version Head; if valid, native failure is reported without guessing browser status. Mapping is fixed: expired→reacquire once; alternate→consume once; configuration→storage details; capacity/native→one user Retry; alternate failure→stop.

`AssetImagePropsV1` is `{assetRevisionId, variant, alt, focusTargetId, className?}`. Shared component renders image plus persistent `View asset details` anchor with caller-unique ID and canonical revision route. No caller supplies URL/retry code. It diagnoses img errors and follows only server action. Automatic reacquire/alternate happen once per generation. User Retry starts a new generation; failure retains button focus; success announces then focuses owned anchor.

ImageReview primary uses content. Filmstrip, CaptionItem, Model eval strip, Gallery AssetGrid, Eval comparison, Grid cell and SampleViewer use thumbnail/512. All eight read DTOs and JSX callsites must provide `asset_revision_id`; contract tests fail if any lacks it.

Direct presign requires current versioned-read. Conditional proxy validates ETag/size before first byte; otherwise spool/hash before response. Clean cutover removes legacy routes/`assetUrl`, updates callers/tests and forbids public AssetLocation writes. Delivery slots eight global/two source, 512 MiB, five minutes; disconnect cancels.
## Thumbnail policy

Remote thumbnail fetch is bounded to 256 MiB; unknown streams reserve full first. Decode limits: 32,768 pixels/dimension, 100 megapixels, 512 MiB memory. Exact VersionId/conditional GET plus hash rejects drift. Output is revision-keyed WebP in a registered cache LocalRoot, never AssetLocation. Before I/O, reserve 256 MiB temporary plus 16 MiB permanent worst case under per-key lock. The cache index tracks complete/inflight entries, measured bytes, last delivery, lease, and generation. Capacity pressure evicts least-recently-delivered complete unleased derivatives deterministically (created ID tie-break), fsyncs deletion, then releases measured bytes; if still insufficient return typed 429 delivery_capacity without decode. Account thumbs, temp, partials, and foreign bytes in cache-root/filesystem headroom. Temporary originals/partials are removed after publication or failure.

# Operator Flows

## Connect/verify writable S3

1. Advanced source form collects friendly name, HTTPS endpoint or AWS default, bucket, allowed prefixes, non-empty managed prefix, region/addressing, credential environment prefix.
2. Save creates a candidate source.
3. Verify runs one single-flight/cooldown capability probe.
4. Probe completion re-counts all configured candidates atomically; only a sole candidate may auto-pin.
5. Success reports Write ready. Failure reports exact redacted capability and leaves source import-only.
6. With several candidates, Settings says `Choose an S3 account to switch` regardless of probe completion order.

## Recovery by admission state and typed return drafts

Only pre-admission resources may create a server-side `StorageReturnDraftV1`. `POST /api/storage-return-drafts` validates closed kind payload and stores token hash, workspace/profile, referenced pre-admission resource/version, invoker ID, 30-minute expiry, state open; returns random 256-bit token. Raw token appears only in hash route `#/settings?panel=storage&focus=recovery&return=...`.

Settings parses the hash query and `POST /api/storage-return-drafts/resolve` with token body to read without consuming. After operator choice, `POST .../consume` CAS-validates open/unexpired/same resource version and returns validated draft plus destination route; token becomes consumed. Any admission CAS invalidates related drafts first, so admitted work cannot recover through them. Token survives reload in SQLite; raw token never persists/logs. Missing/corrupt/expired/replayed/invalidated yields typed code, usable Settings, recovery-heading focus, announcement, and safe Start-new link.

Closed drafts:

- import preview: preview ID/version/source/project/prefix/kind/mode; invoker `start-import`;
- FAL admission: admission ID/version/endpoint/checkpoint revision/subject definition/validated parameters/policy; invoker `start-fal-run`;
- migration preflight: preflight ID/version/scope/destination; invoker `start-migration`.

Pre-admission may reverify/choose replacement/Local. Admitted migration only identity-equal reverifies; replacement creates successor. Admitted FAL follows frozen fallback/fail. Admitted import freezes origin/mode; drift requires new preview. Asset repair never uses return drafts.
## Import parent/items, preview, continuation, and source outage

`StorageImport` owns preview/snapshot/source/mode/root, version/state/job/reservation, counts and `#/transfers/imports/{id}`. Ordered item freezes source object/revision/size/ordinal/root/transfer/outcome/generation. Outcomes include migration states plus capacity_blocked/source_unavailable.

APIs: create/admit ImportPreviewV1; GET parent detail; POST actions with expected parent version and `cancel|continue_incomplete|retry_failed|review_changed|reverify_source`.

`StorageImportActionResultV1` closed union:

- `detail`: current parent detail, canonical route, focus/announcement;
- `preview`: fresh ImportPreviewV1 plus `#/import/previews/{id}`;
- `conflict`: 409 current parent or current preview plus canonical route/focus/announcement.

Initial detail focuses `import_route_heading` and politely announces state/count. User success focuses `import_status` with assertive result; stale conflict focuses `import_action_error`; capacity partial focuses status; source outage focuses `import_source_recovery`; fresh-preview transition focuses `start-import` and asserts `Source or scope changed; review the new import preview.`

Keep requires current versioned/conditional read/no Local reservation. Basic-read-only may Copy. Copy freezes LocalRoot and capacity. Only committed verified Local copies become preferred.

Continue operates only frozen remaining items; Retry only identity-equal retryable items. `reverify_source` verifies the **frozen source identity** and returns same parent detail; replacement/drift instead returns fresh preview. `review_changed`, exhausted source, legacy supersession, or Restart returns preview union. Cancellation follows migration boundaries. Generic retry means fresh preview, never same-parent continuation. Unrelated policy/source absent; legacy hydrate/payload clone removed.
## Storage policy API and change-default flow

`GET /api/storage-policy` returns `StoragePolicyV1`: version, `desired_provider: local|s3`, `resolved_provider`, selection origin, selected write-source identity/null, `fal_local_fallback`, redacted blocked reason, and candidates discriminated as pending|failed|read_only|write_ready with capabilities. It represents all zero/sole-pending/sole-ready/multiple states.

`PATCH /api/storage-policy` accepts expected version, explicit Local or named Write-ready source, and fallback choice. Under `BEGIN IMMEDIATE` it re-evaluates candidates; stale/unready returns 409 `StoragePolicyConflictV1` with current policy/candidates and no mutation. Success returns new projection and announcement class.

`/api/operator-settings.storage.default_source_id` and mutable signed-URL TTL are removed as policy authority; all callers migrate. Signed TTL is fixed server safety policy. Imports never read image write policy. Existing locations remain; new FAL/upload jobs snapshot policy.
## Migration preview/detail/action contract, execution ownership, and replacement

One `MigrationExecutionOwner(workspace_id,parent_id,owner_epoch)` serializes execution. Active/partial/retryable/canceled-with-remaining parent retains it. Admission and continuation validate it under BEGIN IMMEDIATE. Each child transfer captures parent/epoch.

Endpoints: `POST /api/storage-migrations/preview`; `POST /api/storage-migrations/admit`; `GET /api/storage-migrations/{id}`; `POST /api/storage-migrations/{id}/actions`. Canonical detail route: `#/transfers/migrations/<uuid>`.

```text
StorageMigrationScopeV1 =
  | {kind:"all_images"}
  | {kind:"project", project_id:UUID}
  | {kind:"unassigned"}
StorageDestinationV1 =
  | {kind:"local", root_id:UUID}
  | {kind:"s3", source_id:UUID}
StorageMigrationStateV1 =
  "queued"|"running"|"cancel_requested"|"partial"|"retryable"|
  "capacity_blocked"|"canceled_with_remaining"|"completed"|"failed"|
  "abandoned"|"superseded_destination"|"superseded_source_changed"
StorageMigrationReasonCodeV1 =
  "capacity"|"source_changed"|"destination_unavailable"|
  "transfer_failed"|"operator_canceled"|"storage_contract_mismatch"
StorageMigrationFrozenFieldsV1 = {
  workspace_id:UUID,
  scope:StorageMigrationScopeV1,
  destination:StorageDestinationV1,
  source_snapshot_ids:UUID[],
  source_inventory_digest:string,
  ordered_asset_digest:string,
  policy_version:int,
  created_at:string
}
StorageMigrationCountsV1 = {
  eligible_items:int, excluded_items:int, terminal_items:int,
  committed_items:int, failed_items:int, remaining_items:int,
  unknown_size_items:int, known_bytes_total:int,
  committed_bytes:int, inflight_bytes:int, worst_case_bytes:int
}
StorageMigrationPreviewReasonCodeV1 =
  "no_eligible_images"|"destination_not_ready"|"capacity"|
  "source_unavailable"|"replacement_conflict"
StorageMigrationPreviewReasonV1 = {
  code:StorageMigrationPreviewReasonCodeV1,
  redacted_label:string, affected_items:int
}
StorageMigrationPreviewV1 = {
  id:UUID, version:int, expires_at:string,
  frozen_fields:StorageMigrationFrozenFieldsV1,
  counts:StorageMigrationCountsV1,
  readiness:"ready"|"blocked",
  reasons:StorageMigrationPreviewReasonV1[],
  admit_enabled:boolean,
  disabled_reason_code:StorageMigrationPreviewReasonCodeV1|null,
  replaces_parent_id:UUID|null,
  expected_parent_version:int|null,
  expected_owner_epoch:int|null,
  focus_target_id:string
}
StorageMigrationPreviewRequestV1 = {
  scope:StorageMigrationScopeV1,
  destination:StorageDestinationV1,
  replacement_parent_id:UUID|null,
  expected_parent_version:int|null
}
StorageMigrationAdmitRequestV1 = {
  preview_id:UUID, expected_preview_version:int
}
StorageMigrationHistoryV1 = {
  sequence:int, at:string,
  event_code:"admitted"|"started"|"item_committed"|"item_failed"|
             "cancel_requested"|"capacity_blocked"|"continued"|
             "abandoned"|"superseded"|"completed",
  redacted_summary:string
}
StorageMigrationActionNameV1 =
  "cancel"|"resume_remaining"|"retry_failed"|"continue_incomplete"|
  "review_changed"|"replace_destination"|"abandon_remaining"
StorageMigrationControlV1 = {
  action:StorageMigrationActionNameV1,
  label:string, enabled:boolean,
  disabled_reason_code:StorageMigrationReasonCodeV1|null
}
StorageMigrationReplacementLinkV1 = {
  relation:"predecessor"|"successor", migration_id:UUID, route:string
}
StorageMigrationDetailV1 = {
  id:UUID, version:int, canonical_route:string,
  state:StorageMigrationStateV1,
  reason_code:StorageMigrationReasonCodeV1|null,
  frozen_fields:StorageMigrationFrozenFieldsV1,
  counts:StorageMigrationCountsV1,
  progress:{value:int,max:int},
  history:StorageMigrationHistoryV1[],
  controls:StorageMigrationControlV1[],
  replacement_links:StorageMigrationReplacementLinkV1[],
  focus_target_id:string
}
StorageMigrationActionRequestV1 =
  | {action:"cancel", expected_version:int}
  | {action:"resume_remaining", expected_version:int}
  | {action:"retry_failed", expected_version:int}
  | {action:"continue_incomplete", expected_version:int}
  | {action:"review_changed", expected_version:int}
  | {action:"replace_destination", expected_version:int,
     preview_id:UUID, preview_version:int}
  | {action:"abandon_remaining", expected_version:int}
StorageMigrationAdmitResultV1 =
  | {kind:"accepted", detail:StorageMigrationDetailV1,
     announcement:AnnouncementV1}
  | {kind:"conflict", current:StorageMigrationDetailV1,
     winner_route:string, focus_target_id:string,
     announcement:AnnouncementV1}
StorageMigrationActionResultV1 =
  | {kind:"detail", detail:StorageMigrationDetailV1,
     announcement:AnnouncementV1}
  | {kind:"preview", preview:StorageMigrationPreviewV1,
     destination_route:string, focus_target_id:string,
     announcement:AnnouncementV1}
  | {kind:"conflict", current:StorageMigrationDetailV1,
     winner_route:string, focus_target_id:string,
     announcement:AnnouncementV1}
```

Preview returns 200. Admit returns 202 accepted or 409 conflict. Actions return 200 detail/preview or 409 conflict. `AnnouncementV1` is `{event_id,mode:"polite"|"assertive",message,focus_target_id:null|string}`. Native progress uses terminal_items/eligible_items; visible byte text uses known/committed/inflight/worst plus unknown-size count. Server controls are exhaustive authority.

Initial detail focuses heading then polite count; background update is polite/no focus; user action is assertive; conflict is assertive error/winner link. `review_changed` returns fresh Transfers preview while predecessor retains owner. `replace_destination` consumes predecessor-bound preview.

Review-successor/destination replacement CASes preview/predecessor/owner, bumps epoch, revokes predecessor transfer generations, marks reservations reclaim-pending, terminalizes predecessor, links parents and installs successor. Abandon performs revocation then releases owner. Reservations wait for FD reap. Old workers require transfer+owner fences through final commit; stale bytes clean exactly or quarantine. Concurrent tabs get 409 and cannot create second parent. Terminal parent has no resume.
## Persist FAL-generated images

### Unified admission and clean caller cutover

Prompt evals and grids use one `FalAdmissionV1` resource with discriminated `kind`; no parallel grid admission API.

```text
FalImageSizeV1 =
  "square_hd"|"square"|"portrait_4_3"|"portrait_16_9"|
  "landscape_4_3"|"landscape_16_9"|
  {width:int,height:int}
IdeogramParametersV1 = {
  endpoint_id:"ideogram/v4/lora", image_size:FalImageSizeV1,
  expansion_model:"None"|"Medium"|"Large",
  rendering_speed:"TURBO"|"BALANCED"|"QUALITY",
  acceleration:"none"|"low"|"regular"|"high",
  num_images:1|2|3|4, seed:int|null, lora_scale:number,
  enable_safety_checker:boolean, output_format:"jpeg"|"png"
}
KreaParametersV1 = {
  endpoint_id:"fal-ai/krea-2/turbo/lora", image_size:FalImageSizeV1,
  acceleration:"none"|"regular", enable_prompt_expansion:boolean,
  num_images:1|2|3|4, seed:int|null, lora_scale:number,
  enable_safety_checker:boolean, output_format:"jpeg"|"png"
}
FalParametersV1 = IdeogramParametersV1 | KreaParametersV1
FalCreateAdmissionRequestV1 =
  | {kind:"eval", mode:"single"|"prompt_batch", project_id:UUID,
     model_version_id:UUID, checkpoint_revision_id:UUID,
     prompts:[{prompt_id:string,prompt:string}],
     parameters:FalParametersV1, client_request_id:UUID}
  | {kind:"grid", project_id:UUID, model_version_id:UUID,
     checkpoint_revision_id:UUID, grid_definition_id:UUID,
     parameters:FalParametersV1, client_request_id:UUID}
FalSubjectV1 = {
  id:UUID, kind:"single"|"prompt"|"grid_cell",
  stable_key:string, prompt_id:string|null, grid_cell_id:UUID|null,
  input_digest:string, expected_output_count:int,
  admitted_ordinals:int[]
}
FalAdmissionStateV1 =
  "unsubmitted"|"blocked"|"ready"|"admitted"|
  "canceled"|"expired"|"superseded"
FalAdmissionControlV1 = {
  action:"admit"|"cancel"|"configure_storage"|"repair_checkpoint_source",
  enabled:boolean, disabled_reason_code:string|null
}
FalAdmissionV1 = {
  id:UUID, version:int, kind:"eval"|"grid",
  state:FalAdmissionStateV1,
  frozen:{workspace_id:UUID,project_id:UUID,model_version_id:UUID,
          checkpoint_revision_id:UUID,endpoint_id:string,
          adapter_version:string,input_digest:string,
          normalized_parameters:FalParametersV1,
          storage_policy_version:int,spool_root_id:UUID},
  subjects:FalSubjectV1[],
  expected_artifact_count:int,
  expected_max_bytes:int,
  handoff_assurance:"locally_verified"|"provider_verified",
  billing_disclosure:string|null,
  required_acknowledgement:string|null,
  block_reasons:[{code:string,redacted_label:string}],
  recovery_links:[{kind:"storage"|"checkpoint",href:string}],
  controls:FalAdmissionControlV1[],
  canonical_route:string,
  focus_target_id:string
}
FalAdmitRequestV1 = {
  expected_version:int,
  billing_acknowledgement:
    "I understand FAL may bill this run even if checkpoint fetch fails"|null
}
FalAdmissionResultV1 =
  | {kind:"projection", admission:FalAdmissionV1,
     announcement:AnnouncementV1}
  | {kind:"accepted", admission:FalAdmissionV1, eval_run_id:UUID,
     canonical_route:string, focus_target_id:string,
     announcement:AnnouncementV1}
  | {kind:"conflict", current:FalAdmissionV1,
     canonical_route:string, focus_target_id:string,
     announcement:AnnouncementV1}
```

`POST /api/fal-admissions` takes create union and returns 200 projection. `GET /api/fal-admissions/{id}` returns projection. `POST /api/fal-admissions/{id}/admit` takes `FalAdmitRequestV1`, returns 202 accepted or 409 projection/conflict. Canonical draft route is `#/evals/admissions/<id>`; accepted route is `#/evals/<eval_run_id>`. Blocked storage/checkpoint/capacity returns 200 projection with server controls/links and focus. Locally verified admission exposes exact disclosure and accepts the exact acknowledgement only for that current admission version; provider-verified requires null.

Create validates endpoint-specific closed parameters, freezes model/checkpoint, prompt list or grid definition/cells, adapter version, storage policy, handoff and capacity. Eval single requires exactly one prompt. Prompt-batch IDs are unique/nonempty. Grid cells are precreated from frozen definition.

Stable keys: single `(admission,single,admission)`; prompt `(admission,prompt,prompt_id,input_digest)`; grid `(admission,grid_cell,definition,x,y,axis_digest)`. Single/prompt expected count is validated `num_images` (current adapters 1–4; global hard max 16). Grid cell count is 1. Persist ordinals `0..count-1`; admission sum ≤100 artifacts/10 GiB before billing.

Admit under BEGIN IMMEDIATE revalidates version/gates/policy/checkpoint/handoff/reservations, materializes EvalRun/all subjects/intents, marks post-marker effect epoch, then commits before POST. Intent unique by subject/generation. Same-request resume is nonbillable; selected-subject retry increments generation with acknowledgement. Dispatch uses owner/version/fence and provider idempotency; unknown submit reconciles or blocks ambiguous.

Every admission freezes spool grants before POST. Local/fallback also reserves permanent output; S3 needs current write proof. Checkpoint is exact version with current locally/provider-verified handoff or explicit verified staging. A locally-verified checkpoint-fetch failure after acknowledgement is terminal partial/failed; any new generation can bill again.

Remove direct provider-producing `POST /api/eval-runs` and `POST /api/grid-definitions/{id}/runs` from OpenAPI/callers in the same release. Web eval/grid submit and CLI commands create admission, render projection/disclosure, then admit. No legacy route can bypass review, acknowledgement, capacity or effect-epoch admission.

### Canonical provider result and artifact observation

Adapter `ArtifactIdentityV1` uses immutable provider artifact ID or stable admitted ordinal plus `(mime,width,height,seed-or-null)`; URL/path/query/fragment never participates/persists. Canonical ordered JSON list digest must contain exactly expected identities before fetch. Missing/extra/duplicate/malformed terminally blocks `provider_result_shape`.

Persist one receipt `(subject,request,ordinal)` with identity/metadata digest, observed size/SHA, stage identity/fence, transfer/output. Credential-free SafeHttp artifact transport uses fresh in-memory URLs, redirect/peer validation, 100 MiB/10-minute/8 MiB bounds and fsync/hash. Crash before observed refetches; after observed reopens/re-hashes. Missing stage re-gets result through canonical FalApiTransport, recomputes list digest, uniquely matches identity and requires recorded SHA. Mismatch quarantines/blocks; no Asset.

Commit atomically links receipt→transfer→blob/location→EvalOutput; cleanup after current-fence commit. Aggregate run: succeeded all; partial some plus terminal unsuccessful; failed none; blocked ambiguous/shape/changed/quarantined.

### Recovery API

`GET /api/eval-runs/{id}/storage-recovery` returns versioned `FalRecoveryV1` with counts/categories, outputs, subject generations, cause/action codes, controls and focus. `POST .../storage-actions` supports same-request selected resume or acknowledged selected terminal-generation retry; success/409 returns current projection/route/announcement and conflict creates no intent.

`POST /api/eval-runs/{id}/new-admission` takes `{source_recovery_version}` and copies only allowlisted immutable inputs into a fresh `FalAdmissionV1`; all policy/checkpoint/storage checks rerun. Configure-future navigates Settings. Payload clone/direct old retry is prohibited.
## FAL checkpoint handoff

EvalRun pins CheckpointRevision/location and handoff assurance. Exact VersionId presign is locally fetched/hashed through SafeHttpTransport before billing; explicit staging does the same after verified copy. Provider-verified mode is used only when an adapter actually supplies a documented non-billable fetch preflight. Current adapters use acknowledged locally-verified mode. Retry stays original handoff; unavailable requires new generation/run and repeats disclosure.
## Asset storage/recovery contract

`GET /api/assets/{id}/storage` returns versioned `AssetRecoveryV1`: asset/revision/proof version, preferred/actual/alternate projections, cause/action codes, stable summary ID, and discriminated eligible actions. Server includes only validated candidates and redacted labels:

- reverify_source `{bound_source:{id,label}}`, parameterless;
- bind_replacement_source `{candidates:[{source_id,label,compatibility:"candidate"}]}`;
- copy_current_root `{root:{root_id,label},from_location_id}`;
- verify_restore_root `{root:{root_id,label,expected_fingerprint_hint}}`, no path;
- reimport `{route,source_candidates:[{source_id,label}]}`.

`POST /api/assets/{id}/storage-actions` uses closed discriminated body with expected revision/proof: reverify has no extra input; bind sends candidate source ID; copy sends offered root/from IDs; restore sends offered root ID; reimport navigates rather than mutates. Unoffered IDs are 400.

Result union: 200 updated projection; 202 `AssetRecoveryJobV1 {job_id,detail_route,projection,announcement}`; 409 current projection plus conflict code. Async detail route is canonical job route. Every response names summary focus ID and action-class polite/assertive announcement. Exact observation/SHA required to bind replacement; exact fingerprint to restore root. Controls stay ImageReview StorageDetails; AssetImage configuration links here.
# UI and Accessibility

## Asset storage detail surfaces

V1 has one canonical asset-detail route: `#/image/<asset-uuid>`. `StorageDetails` lives in `ImageReviewScreen`’s dedicated inspector at widths above 820 px and as a non-modal inline semantic disclosure below the image at the existing 820 px breakpoint. `View storage details` expands, scrolls, and focuses its stable summary heading. Dense cards/cells link `View asset details` to this route. The shell `ContextInspector` does not host asset StorageDetails in v1; no new 1180 px asset dialog is introduced.

Primary storage/provenance summary stays visible. Copies and Technical details are separate disclosures; failure/repair auto-expands Copies only. Technical details hides absolute paths, secrets, and signed URLs.
## Image-surface accessibility and announcements

Every dense surface renders a persistent `View asset details` anchor per tile/cell; this is the uniform post-success Retry focus target.

Mounted batch provider owns pre-mounted `role=status aria-live=polite aria-atomic=true`. Event identity is `(assetId, contentRevision, retryGeneration, terminalOutcome)`. A two-second window announces each attempt once. Starting Retry increments generation and cancels any unflushed prior-generation event for that asset; already announced history stays. Scope change/unmount cancels timers and discards unannounced events. Retry success announces then focuses persistent anchor; Retry failure retains button focus. Background events never move focus.

ImageReview uses inspector at min-width 821 px and native details/summary inline at max-width 820 px; 819/820/821 tests apply. Always-visible fields: current serving outcome, preferred/actual source, content revision/verification, redacted cause, then action/disclosures.
## Progress and recovery accessibility

Migration uses native progress/determinate semantics and visible item/byte text. Polite live regions announce queued/running milestones/terminal state. Focused assertive notices are reserved for user-triggered cancel/recovery/repair results or explicit navigation to failures. Controls are keyboard-operable and errors are redacted. Canonical migration and recovery focus rules above are normative.
# Security and Resource Boundaries

## No-auth launch and HTTP checks

Remove importable module-level ASGI `app`. Packaged launcher pre-binds validated loopback socket/owner-only UDS and constructs `create_app(LocalLaunchContract)` with an in-memory non-serializable contract; generic Uvicorn import cannot start it. Start Uvicorn with proxy headers disabled and no forwarded allowlist.

Earliest middleware reads raw transport peer before proxy middleware, rejects all Forwarded/X-Forwarded headers, duplicate/malformed Host, nonloopback peers, and invalid browser Origin. Exact configured Host (with normalized port) is required; state-changing browser Origin is exact allowlist. Wildcard/LAN/reverse-proxy/port-forward modes are unsupported and rejected where detectable. This remains a trusted single-user host-wide boundary, not authentication.
## Outbound transports, credentials, bounds, and sanitization

Credential epoch includes access key/secret/session token presence/value/expiry; change/restart invalidates clients/capabilities.

`SafeHttpTransport` enforces HTTPS/no-userinfo, no ambient proxy/credentials, all A/AAAA public-address policy, and actual connected-peer validation on initial connect and every redirect/reconnect. Any non-public answer or peer rejects before request.

S3 injected session connects validated IP while preserving SNI/Host/SigV4; redirects disabled. FalApiTransport constructs canonical-origin lifecycle URLs from strict request ID and sends Authorization only to exact origin; cross-origin/downgrade rejects.

ArtifactFetchTransport is built on SafeHttpTransport and applies the same initial/redirect/reconnect/DNS/peer rules, plus no credentials/cookies/proxy. Public→private rebinding and private/loopback/link-local targets reject.

FalApiTransport per request: connect 10 s, read 30 s, total 60 s; success max 2 MiB, error 64 KiB; JSON nesting 16, ≤512 keys, ≤100 artifact descriptors, strings ≤64 KiB, duplicate keys rejected, strict typed top level. Streaming bounded decode occurs before object construction. Oversize/malformed status/result becomes redacted terminal provider_result_shape/transport, creates no receipt/new POST, and releases reservations after physical-writer proof. Unknown submit response alone uses reconciliation/ambiguous rule.

Typed sanitizers reject URLs/raw responses/Authorization/secrets/userinfo before generic persistence; structural redactor covers bounded nested/cyclic legacy state, logs, telemetry, errors, backup metadata.
## Managed keys, writes, and cleanup

```text
{managed-prefix}/workspaces/{workspace-uuid}/
  projects/{project-uuid}/source/{transfer-uuid}/{attempt-uuid}
  projects/{project-uuid}/generated/{transfer-uuid}/{attempt-uuid}
  unassigned/{asset-or-transfer-uuid}/{attempt-uuid}
  probes/{random-probe-uuid}
```

Only server UUIDs enter keys. Builder rejects empty/absolute/dot/traversal and proves segment-boundary descent. Private single-Put create-only, max 2 GiB; no multipart/ACL/public mutation.

Automatic remote deletion requires current cleanup capability and exact recorded verified VersionId/equivalent atomic condition. Persist CleanupReceipt for exact deleted version. Missing/replaced/foreign is no-op quarantine. On unversioned/no-conditional-delete sources, old-fence/failed attempt objects remain retained quarantine forever unless operator cleans externally; never issue unqualified DeleteObject.
## Signed URLs and redaction

Browser GET-only presigns default five minutes/hard max 15; checkpoint API max 24 hours only for immutable revision-bound objects. Responses use `Cache-Control: private, no-store`; image elements use `Referrer-Policy: no-referrer`.

Typed URLs lose query/fragment before generic serialization. Structural redactor recursively covers job/snapshot/transfer/migration payload/result/error, legacy migration, activity, provider responses, exception chains, API errors, telemetry, and logs. It removes signed queries, Authorization, credentials, endpoint userinfo, and secret-like fields.

## Aggregate admission leases and capacity

Limits: background transfers 4 global/2 source; delivery 8/2; artifact fetch 4 global/2 EvalRun; FAL POST 4/2 endpoint; queued writes 100/workspace/25 source. Per subject ≤16 artifacts; grid ≤100 subjects; EvalRun ≤100 artifacts/10 GiB; artifact 100 MiB. Local root quota 100 GiB; cache root quota 10 GiB; shared headroom max(10 GiB,10% FS); temp 10 GiB. Item 2 GiB; thumbnail source/temp 256 MiB and output 16 MiB; proxy 512 MiB; probe 1 KiB/min/source.

Admission under BEGIN IMMEDIATE reserves permanent worst case plus named spool grants. Thumbnail admission reserves cache temp/permanent and may evict only complete unleased entries before rejection. Filesystem-wide accounting reserves DB and backup headroom before any root quota. FAL spool is `100 MiB × min(2,total expected)`; unknown item 2 GiB.

Release requires DB terminal fence plus supervisor proof writer/FD reaped; expiry marks reclaim-pending. ≤8 MiB chunks validate grant. ENOSPC closes/unlinks/fsyncs, records noncommit, then releases measured bytes. Cache eviction follows the same delete/reap rule. FAL cannot POST without active output limit, spool grant, and permanent reservation.
# Requirements

1. Timing-independent versioned StoragePolicy API; explicit Local/S3 and FAL fallback; legacy policy retired.
2. Current source capability proofs for write, conditional/versioned delivery, cleanup, and locally/provider-verified FAL handoff assurance.
3. Immutable source observation and one-time ContentBlob verification; changed bytes create new revision.
4. Exact preferred location and same-blob fallback/copy only.
5. Source/location/revision binding covers run, checkpoint, model, eval, refresh, hydration, handoff, delivery, copy.
6. Canonical immutable source fingerprint; identity changes create replacement.
7. Descriptor-relative durable LocalRoot publication with fsync ordering and physical writer fencing.
8. PublicationAttempt/receipt fences; stale attempts never promote; foreign/unversioned bytes never auto-delete.
9. Typed snapshot/job contract, closed protected producers, gates before persistence/recovery/claim.
10. Effect-named two-phase import; Keep requires safe remote delivery; Copy freezes inventory/root/capacity.
11. Mandatory SHA and authoritative destination verification before copy preference.
12. SQL-defined migration scope/order, CAS conflict/replacement, item revision, commit/cancel/continuation.
13. Versioned AssetDeliveryDescriptor/AssetImage contracts replace every legacy image URL.
14. Numeric thumbnail/delivery/resource bounds and derivative-only cache.
15. Pre-materialized FAL subjects, at-most-once intents, stable generation scope, receipt aggregates.
16. Crash-safe bounded generated-artifact observation stage before StorageTransfer/EvalOutput.
17. Exact-version locally verified FAL handoff with acknowledged billing risk; provider proof only where supported.
18. Isolated named S3 credentials and split hardened FAL API/artifact transports.
19. Durable leases/CAS plus permanent/temp reservations not released before physical writer reap.
20. Trusted single-user host-wide no-auth launch contract with raw peer/Host/Origin checks.
21. Structural redaction before generic persistence/log/UI/backup metadata.
22. Versioned policy/import/migration/asset-recovery/FAL-recovery/return-draft API contracts.
23. Canonical ImageReview StorageDetails/migration routes and deterministic focus/announcements.
24. Frozen empty/unversioned/0001/0002 migration baselines; unknown shapes unmodified.
25. Exclusive cutover ownership, verified crash-safe encrypted backup, armed journal, no pre-marker resume.
26. One complete compatibility artifact; feature gates enabled only after installed-data verification.
27. Launcher-owned storage-contract verification receipt precedes every dependent gate and is mandatory after restore.
28. Raw/sanitized restore replaces main/WAL/SHM as one journaled set; sanitized rollback is pre-enable/no-effect only.
29. Asset revision, delivery and migration read/write DTOs are closed and shared across every browser caller.
30. Unified two-step FAL admission replaces direct eval/grid provider submission and freezes exact subjects/counts.

# Acceptance Examples

1. A/B probe completion order cannot choose an automatic account; sole configured ready A can.
2. FAL S3 success stores A; pre-admission unready A blocks; post-admission outage stores Local only when captured fallback allows; next new job retries A.
3. Healthy B Keep/Copy imports work while unrelated A is stale and snapshot omits A.
4. Scope SQL uses direct workspace/project fields and created-at/ID order; indirect references do not recategorize Unassigned.
5. Concurrent migration enqueue creates one parent/policy transaction; loser gets 409 existing canonical URL and no policy change.
6. Remote observation change creates new Asset; SHA attaches/reuses ContentBlob without replacing observation or historical references.
7. Published dataset version, draft, checkpoint revision, ModelVersion, EvalRun, Sample, EvalOutput, review/comment follow the propagation matrix idempotently.
8. Cancel before atomic commit yields remaining; verified transfer atomically commits transfer/location/blob/preference/item/reservation; restart agrees.
9. Versioned delivery presigns exact version; unversioned managed/external delivery conditionally proxies.
10. FAL handoff blocks unless exact VersionId; unversioned checkpoint is copied to verified versioned destination first.
11. Missing named S3 credentials makes no DNS/metadata call and sends no unrelated Authorization.
12. Admission power loss expires/reclaims with higher fence; stale owner cannot submit/publish/commit.
13. Unknown/malformed version-0 job is superseded; wrong new job quarantined; neither requeues.
14. Import retry, run refresh, hydrate, eval/grid retry, migration, descriptor direct POST all enforce named gates before persistence.
15. Raw readiness/job URLs are scrubbed; encrypted raw backup expires/purges; restore re-scrubs offline before startup.
16. `#/transfers/migrations/<uuid>` is the only detail route and renders exact state controls/focus.
17. Missing/expired/corrupt recovery record leaves recovery usable, announces loss, and offers safe new-operation link.
18. Dense batch announcements obey exact key/window/reset message and never move focus.
19. Source isolation matrix includes TrainingRun and CheckpointRevision.
20. Same release performs schema/repository/backfill/disposition before marker and before worker recovery.

21. Versioned-read and conditional-read probes fail closed against providers that fake VersionId or ignore If-Match.
22. Publication intent/receipt recovers crash before post-write DB update without deleting foreign bytes.
23. Generated receipt and FAL submission intent make artifact/billable recovery idempotent or terminally ambiguous.
24. Legacy conversion matrix preserves every foreign key and never manufactures byte equivalence.
25. SQLite BEGIN IMMEDIATE/CAS rules serialize policy, migration, leases, and atomic transfer commit.
26. LocalRoot uses descriptor-relative owner/mode/link-safe operations.
27. Recovery matrix distinguishes pre-admission drafts, admitted frozen work, source/root repair, and unretained FAL result.
28. Launcher-owned raw-peer listener and bounded delivery proxy enforce no-auth/network/resource contracts.

29. Publication attempts never cross fence generations; reclaimed work uses a new key and quarantines old-fence bytes.
30. Local FAL output needs Local capacity but no S3 output source; unversioned checkpoint staging is explicit and separate.
31. FAL intent uniqueness/CAS and parent partial aggregation prevent duplicate billing/output.
32. Descriptor/alternate/byte HTTP APIs replace old content/thumbnail routes in one caller cutover.
33. Admitted import transient outage, migration destination replacement, and asset in-place recovery have named terminal/resume semantics.
34. SQLite backup API plus verified streaming AEAD envelope is the only cutover backup format.
35. Permanent/temp capacity is reserved before billable or Local work using fixed numeric limits.
36. Keep-at-origin basic-only source blocks before job/catalog; Copy-to-local admits only after frozen-inventory reservation.
37. Descriptor/client state machine distinguishes expiry, alternate, configuration, capacity, and explicit Retry without loops.
38. All FAL subjects exist before POST; only selected failed subjects receive explicit billable new generations.
39. Generated bytes are durably observed and hashed before transfer; refetch mismatch never creates an Asset.
40. Unfinished armed cutover never resumes or serves; verified raw restore is the sole recovery.
41. FAL Authorization never leaves canonical API origin; artifact fetch receives no credentials.
42. Reservation expiry cannot free blocks still held by a stale Local writer.

43. Same-parent import Continue processes only frozen remaining items; fresh-preview retry never substitutes it.
44. Thumbnail/content variant DTOs map all eight surfaces and bind exact derivative/original metadata.
45. Post-img failure diagnosis uses server-authored codes; the browser never guesses HTTP status.
46. Migration preview/admit/action conflicts return versioned detail, canonical winner route, focus and announcement.
47. FAL recovery CAS distinguishes same-request resume from acknowledged selected-subject billing.
48. Asset recovery offers only server-validated redacted candidates with discriminated action inputs.
49. Sanitized post-validation backup is independently verified/bound before marker and purge-receipted after seven days.
50. Direct public asset/location creation is absent; only repository publication creates a physical location.
51. Provider result cardinality equals admitted expected artifact count before any fetch.
52. Artifact recovery matches canonical non-secret identity/list and recorded SHA.
53. Every FAL admission holds mandatory frozen spool-root slots before provider submission.
54. Partial/canceled resumable migration retains workspace execution ownership until complete, abandon, or atomic replacement.
55. Import actions return closed detail/preview/conflict unions with frozen-source reverify and deterministic focus/announcements.
56. Artifact fetch rejects initial/redirected/rebound non-public peers through SafeHttpTransport.
57. Armed pre-marker journal suspends raw backup purge past 24 hours until recovery/marker outcome.
58. Oversize/malformed FAL control results terminate without crash, new POST, or leaked reservation.
59. Current FAL admissions require locally verified exact-version handoff plus explicit post-billing-fetch-risk acknowledgement.
60. Migration owner epoch fences every predecessor worker transfer before and after replacement/abandon.
61. Thumbnail cache quota/eviction preserves shared DB/backup headroom under concurrency and ENOSPC.
62. Sanitized rollback reconstructs exact marker/receipt with gates disabled and is idempotent across every swap boundary.
63. Delivery/alternate/diagnose closed unions drive one shared AssetImage retry/link/focus state machine.
64. Migration detail server controls and closed action result unions cover every action, stale conflict, and replacement link.
65. Storage-contract verification is idempotent, stale-run-lease safe, and the first transition after cutover/restore.
66. Restore with nonempty old WAL/SHM is crash-safe; sanitized rollback after any gate/effect/FAL/transfer rejects without mutation.
67. Asset.id is the revision ID returned by every image read DTO and bound by descriptor/alternate/diagnose.
68. Migration preview/admit/detail/action DTOs expose closed state, counts, progress, reasons, controls and 200/202/409 outcomes.
69. Eval/grid create one FAL admission; exact acknowledgement/current version precedes 202 and old POST routes cannot submit.





# Test Strategy

## Migration/deployment

- Commit/checksum frozen empty, unversioned legacy, stamped-0001, and stamped-0002 fixtures containing Local/cache paths, ambiguous/bindable duplicate S3, raw readiness URLs, full reference graph, and queued/running known/unknown jobs.
- Temporary-copy each fixture through classifier/adopt-or-upgrade/backfill/propagation/scrub/disposition/validators/raw+sanitized backup/marker; assert exact pre/post IDs/counts, backup binding/purge receipt, and no live-head create_all.
- Crash-inject raw and sanitized online-backup/plaintext→AEAD/finalization plus every armed cutover phase; reconcile orphan key/partial/plaintext, require offline raw restore pre-marker, validate sanitized restore post-marker, and prove API/worker cannot open writable sessions.

## Source/revision/storage

- Duplicate-endpoint matrix records exact client for reconcile, version propagation, delivery proxy/presign, thumbnail, hydration, handoff, migration, run refresh, repair.
- Test canonical fingerprint normalization, isolated credentials/no metadata/provider chain, source drift, LocalRoot drift/no-follow/fstat, ContentBlob one-time attach/collision.
- Test single-Put/read-back, unversioned substitution proxy, exact VersionId, transfer atomic commit/crash/mismatch.

## Gates/jobs/admission

- Route-by-route disabled 409/no row for import, import retry, run refresh, hydrate, eval/grid/retry, migration/continuation, descriptor.
- Generic kind allowlist, arbitrary legacy supersession, mismatch quarantine, worker marker/gate/fence before recovery/claim.
- Lease heartbeat/expiry/reclaim/stale owner at each request/chunk/publish/checkpoint/commit; temp and concurrency budgets.

## Migration/FAL

- SQL scope/order and indirect reference cases; concurrent two-tab parent admission; cancel state boundaries/restart; continuation generations/source changed.
- FAL fallback allowed/prohibited and next-job desired policy; fixed controls/ID/JSON/poll/grid/deadline; proxy env; artifact; immutable CheckpointRevision/handoff; propagation/lineage.

## Web

- Add React DOM then browser runner. Test policy fallback copy, recovery session valid/invalid/focus, effect import, canonical migration route/state controls, ImageReview 820 px inspector/inline details, every direct image surface, exact dense announcement windows/messages, Retry/alt/focus/redaction.

- Add provider fakes that report unusable VersionIds and ignore If-Match; assert no bytes delivered.
- Crash after S3/Local publication before receipt: recover only under still-current fence; after reclaim quarantine old target and publish a new-attempt target; prove foreign collision quarantines.
- FAL crash/timeout after acceptance before response with/without provider idempotency/reconciliation.
- Legacy conversion table cases and exact pre/post FK identity/count queries.
- SQLite two-connection BEGIN IMMEDIATE/CAS busy/stale-fence races.
- LocalRoot root/intermediate/final symlink, hard-link, and replacement races.
- Recovery matrix, typed draft union, admitted-state restrictions, unretained FAL failure, route initial focus, Retry focus/live-region.

- Transfer old-fence publication/late stale writer tests: quarantine old key and publish successor key only.
- Local-output FAL with versioned checkpoint and no S3 output source; explicit staging picker for unversioned checkpoint.
- Unique FAL intent conflict/reclaim/ambiguous reconciliation; partial receipt aggregation/resume/no-duplicate output.
- Exact delivery/alternate/byte endpoint DTO/error/token tests and structural proof old routes/callers are gone.
- Import waiting_source/reverify/idempotent partial catalog; migration superseded_destination successor/CAS.
- SQLite backup API/WAL capture, chunked AEAD tamper/nonce/manifest/decrypt/integrity/restore tests.
- Capacity worst-case reservations, headroom/quota, concurrent writers, chunk overflow, and pre-billing Local fallback rejection.

- StoragePolicyV1 projection/PATCH stale/unready CAS and structural retirement of legacy default/TTL authority.
- ImportPreviewV1 version/fingerprint/capability modes: versioned, conditional, basic-only Keep rejection/no rows, zero/one/many Copy, unknown sizes, competing reservations, partial/cancel/continue/source drift.
- AssetDeliveryDescriptorV1 and AssetImageV1 state/expiry/alternate/retry/error/live/focus tests at all eight caller surfaces; direct capabilities replay until expiry, alternate token one-use/restart-invalid.
- Migration preview/detail/action 200/409 unions, exact three-segment route, replacement winner links/controls/focus.
- AssetRecoveryV1 eligible actions/stale proof 409/async result/refocus and StorageReturnDraft create/resolve/consume/invalidate/reload/token-redaction.
- FAL pre-materialized single/prompt/grid subjects, per-subject generation lineage, at-most-once conflict/ambiguity, fresh admission, partial recovery projection.
- Artifact observation crash before/after hash, stage reopen/missing refetch same SHA, changed-byte quarantine, transfer/output atomic linkage and cleanup.
- Current-epoch conditional/versioned/FAL-handoff probes, pass-then-ignore fake, provider-unreachable preflight/no billing.
- FalApiTransport cross-origin/downgrade/response-URL rejection with zero credential leakage; credential-free artifact redirects.
- Local fsync crash points, supervisor writer reap/reclaim-pending reservation, ENOSPC cleanup, exact-version cleanup versus permanent quarantine.
- StorageImport parent/item detail/action CAS: partial commit, cancel, same-parent Continue/Retry frozen items, and fresh preview only for changed/exhausted/restart.
- Delivery content/thumbnail request union, 256/512/1024 derivative keys, non-image/invalid errors, diagnose server-clock/status mapping and all surface variants.
- Migration PreviewV1 scope/destination/admission conflicts plus focus-target/AnnouncementV1 browser assertions.
- FAL RecoveryV1 GET/action CAS, same-request versus acknowledged billable subjects, conflict no-intent, new-admission validation.
- Asset recovery discriminated candidates/bodies, unoffered-ID rejection, 200/202/409 and async canonical route.
- Exact expected FAL result count/ordinals before fetch; canonical ArtifactIdentity/list digest with URL rotation/reorder/malformed/duplicate/missing/extra cases.
- Mandatory spool root/grant for S3 and Local FAL, two-slot scheduler bound, pre-POST failure, crash reclaim only after FD reap.
- MigrationExecutionOwner admission/resume/continue/retry/abandon/replacement CAS, including old partial/canceled parent versus new tab.
- Import StorageImportActionResultV1 detail/preview/conflict, frozen-source reverify, route/focus/announcement classes.
- Artifact initial/redirect private address and public-to-private DNS-rebinding rejection through actual-peer checks.
- Armed marker-absent raw envelope/key survives >24 hours; marker/recovery starts the bounded purge clock.
- FAL submit/status/result oversized, duplicate-key, over-depth, over-list, malformed JSON tests: terminal redacted outcome, no resubmit/crash, reservations reclaimed.
- FAL locally-verified handoff disclosure/ack validation, checkpoint-fetch failure outcome, repeat disclosure on billable new generation.
- Pause migration after receipt verification, replace/abandon, then resume old Local/S3 worker: owner-aware CAS is zero, no location/preference/item mutation, old bytes cleaned/quarantined, reservation waits for reap.
- Concurrent thumbnail fill, LRU tie ordering, in-use exclusion, foreign-byte accounting, partial cleanup, and ENOSPC preserve cache quota and DB/backup headroom.
- Sanitized restore crash-injects journal/decrypt/marker-receipt transaction/checkpoint/swap/fsync/startup; exact post-swap retry finalizes, mismatch preserves both files, gates remain disabled.
- AssetImage contract tests delivery/alternate body token binding, every failure action, automatic-attempt limits, user Retry focus, and caller-unique detail anchor at all surfaces.
- StorageMigrationActionRequest/Result exhaustive contract tests: allowed controls/history/links, 200 detail/preview, 409 current/winner route/focus/announcement.
- StorageContractVerifier disabled/blocked/migrating/ready transitions, immutable receipt, stale run lease, dependent receipt invalidation, and first post-restore enable.
- Database-set restore with nonempty prior WAL/SHM at pre-quarantine/post-quarantine/post-main-install crashes; no stale replay or SQLite open before journal completion.
- Sanitized restore allowed only all-gates-disabled/effect-zero; exact confirmation; reject after contract ready, accepted FAL and Local/S3 publication with current set unchanged.
- All image read DTOs expose Asset.id as asset_revision_id; descriptor/diagnose exact schema and path/body/token mismatch tests.
- Migration preview/admit 200/202/409 plus closed state/count/progress/reason contracts and unknown discriminator rejection.
- Unified eval single/batch/grid admission projection/admit/conflict/capacity/repair flows, exact subjects/ordinals/counts, ack-version CAS, and structural absence of direct submit routes.
- Remove POST /api/assets and AssetLocationCreate from OpenAPI/callers; repository fixture is the only test seed path.
## Live MEGA S4

Non-default registered marker and explicit enable/HTTPS/bucket/probe-prefix/named-credential prerequisites; skip before client creation. Run deterministic writer tests then randomized conditional Put/read-back/presign-or-proxy/delete/redaction probe.

# Atomic Rollout

One compatibility artifact contains **all** schema, repository, source/capability/transport, backup/cutover, protected producers/workers, policy/import/migration/delivery/FAL backends, caller-migrated UI/CLI, and tests. The following are pre-deploy build milestones, not later partial releases:

1. Freeze legacy migrations/fixtures and implement baseline classifier, launcher lock/run lease, verified backup/journal/recovery, schema/backfill/scrub/job disposition, marker/gates.
2. Implement models/repository/redactor/credentials/network/LocalRoot/revision/transfer/publication/lease/capacity with every feature gate disabled.
3. Implement source-bound import/hydration/refresh/repair; migration; descriptor/client caller cutover; FAL intents/subjects/artifacts/handoff; Settings/Transfers/ImageReview/accessibility, all behind gates.
4. Remove installed-DB create_all, mutable source guessing, raw readiness/URLs, generic protected jobs, old image routes/assetUrl, payload clone retry, and legacy policy authority. Prove source/caller/job inventories empty.
5. Pass deterministic/fake/crash/browser/legacy-fixture suites before packaging.

Deployment/enablement in that same artifact:

6. Stop/reap old processes; acquire lock; backup/classify; arm journal; migrate/backfill/scrub/disposition; verify sanitized backup; commit marker 1. Pre-marker interruption requires raw restore. Start diagnostic-capable API only; worker remains stopped.
7. Under the same launcher lease run `titles storage verify-contract`; atomically record receipt and set `storage_contract` ready. Failure leaves blocked/disabled capabilities and no worker.
8. Start compatible API/worker; verify Local descriptor baseline and source isolation, then set `source_bound_io` ready. `policy_import` remains disabled.
9. Verify all descriptor callers plus S3 conditional/versioned paths; set `remote_delivery`, then `policy_import` with current contract receipt.
10. Verify FAL admission/intent/subject/artifact/capacity/handoff/fallback; set `policy_fal`.
11. Verify migration conflict/cancel/restart/replacement/capacity; set `storage_migration`.
12. UI reads gates only. Run opt-in MEGA smoke. Purge raw within 24 hours after marker or raw-restore/abandon receipt, never armed marker-absent. Retain Local recovery.

At every wave, marker/head/run lease must match; producer gate check happens before persistence and worker gate/snapshot/fence check before recovery/claim. No old binary may coexist.
