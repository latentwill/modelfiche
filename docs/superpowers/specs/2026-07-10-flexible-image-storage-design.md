# Flexible Image Storage Design

**Date:** 2026-07-10
**Status:** User-approved direction; design-review revision 1

## Problem

Titles DAM catalogs remote MEGA S4 objects without downloading large files, but image content and thumbnail routes currently require hydrated local files. Generated FAL images are also local-only. Operators need one simple choice between storing image originals on this computer or in S3-compatible storage. The system must preserve provenance, expose the actual source in metadata, fall back safely, and never delete an existing copy during migration.

## Product Outcome

Routine operation presents one decision:

```text
Store new images in:
  Mega S4 — available everywhere, uses less disk
  This computer — works offline
```

The current release remains a single-workspace, loopback-only operator application. The preference is installation-global for that workspace. App-managed S3 keys still include the workspace ID so a later multi-workspace release does not require a key-layout migration.

## Goals

- Let operators choose Local or S3 as the default storage for new image originals.
- Default to S3 only when exactly one writable S3 source is available; otherwise use Local until the operator chooses an account.
- Apply the resolved policy to imports and FAL outputs.
- Allow an advanced per-import provider override without routine import friction.
- Distinguish an import’s origin source from the default writable destination.
- Identify preferred provider, origin source, physical copies, and fallback state in asset metadata.
- Serve remote images without retaining durable local originals.
- Copy existing images on request without deleting prior locations.
- Organize app-managed S3 objects by workspace and project inside one configured bucket.

## Non-Goals

- Moving SQLite application state to S3.
- Automatically deleting prior local or S3 copies.
- Automatically moving objects after workspace or project renames.
- Creating or deleting buckets as part of workspace lifecycle.
- Replacing explicit checkpoint hydration for filesystem-only tools.
- Adding S3-backed export retention or provisioning a CDN.
- Adding a new general-purpose image upload UI. Future uploads must consume the same policy API when that flow is built.
- Making a private bucket, object, or prefix public.
- Supporting unauthenticated writable storage operations over LAN or public network bindings.

## Terminology

- **Storage preference:** The provider an asset should use first: `local` or `s3`.
- **Physical location:** One durable copy represented by an `AssetLocation`.
- **Origin source:** The S3 connection and object identity from which an imported asset was cataloged.
- **Write destination:** The writable S3 connection used for generated images and explicit migrations.
- **Writable source:** An active S3-compatible source with a managed prefix and a recent successful capability probe for create, read/head, presigned GET, and exact-object delete.
- **Fallback:** A verified alternate physical location used when the preferred location cannot serve an image.
- **Managed prefix:** The S3 prefix reserved for Titles DAM writes. Its default is `titles-dam/managed/`.
- **Unbound legacy location:** An existing S3 location whose owning source cannot be proven from retained data.

## Key Decisions

### Multi-location policy

`Asset` plus `AssetLocation` remains the storage foundation. Each image asset records a preferred provider. Every local or S3 copy remains a separate location. Creating a copy never overwrites provenance or reclassifies the original location.

Every new S3 location has a non-null `ImportSource` reference. S3 location identity is source-scoped: the same bucket/key on two endpoints represents two different locations. Reconciliation, hydration, delivery, migration, and FAL checkpoint resolution must use the bound source rather than infer one from bucket name.

The S3 location uniqueness invariant becomes owning source plus object key. ETag/version, size, and modification time describe the observed object revision. App-managed keys are immutable and create-only, so a managed object key never changes contents.

### Origin and write destination are separate

Jobs persist distinct fields:

- `origin_source_id`: the source being browsed/imported, when applicable;
- `resolved_provider`: `local` or `s3`;
- `write_source_id`: the configured writable destination when the operation creates an S3 object;
- a frozen storage-policy snapshot containing managed prefix and source identity needed for retry.

When importing from source B while Settings names writable source A:

- S3 mode keeps the original object at B, binds provenance to B, and serves from B.
- It does not silently copy to A.
- A is used for FAL outputs and explicit Local-to-S3 or cross-source migrations.
- The preview says `Images will remain in <source B name>.`

Local mode downloads and verifies a local copy while preserving source B as origin.

### Source identity cannot mutate underneath locations

After a source is referenced by any location or transfer, endpoint, region, addressing style, bucket, credential-environment prefix, allowed prefixes, and managed write prefix become immutable. Display name and active status remain editable. Secret rotation occurs behind the same environment-variable names. A changed endpoint, bucket, prefix policy, or credential prefix requires a new source record.

Every source and location is workspace-scoped. Resolver and worker operations require source workspace to equal asset workspace, and require the asset project—when present—to belong to that workspace.

### Conservative legacy compatibility

Legacy locations do not retain endpoint identity. No migration may pretend otherwise.

- An existing image with a valid managed local copy backfills to Local preferred, preserving current serving behavior.
- A legacy S3 location remains unbound unless durable retained evidence already identifies one source exactly.
- Unbound S3 locations are metadata-only: they cannot be presigned, copied, hydrated, selected as migration sources, or used as fallback.
- Remote-only unbound assets show `Source repair required` instead of being resolved by bucket guessing.
- An operator repair action lists same-workspace candidate sources whose bucket and allowed prefix match. Binding succeeds only after `HeadObject` verifies the object key and stored size plus ETag/version when available. The action records attribution and moves the location from unbound to usable without copying bytes.

### Simple account selection

- Zero writable sources: Local is selected; S3 is disabled with `Connect S3 storage`.
- Exactly one writable source and no saved preference: S3 and that source become the effective default.
- Several writable sources and no saved destination: Local remains effective until the operator explicitly selects one account. The UI recommends S3 but never picks an arbitrary destination.
- A saved destination remains selected only while its capability state is current and eligible. If it becomes ineligible, new write-producing actions are blocked with a repair action; an already-running job retains its frozen source snapshot and may fall back to Local only under its explicit failure policy.

Primary Settings shows only provider choice and friendly account name. Endpoint, bucket, managed prefix, addressing style, credential prefix, and signed-URL controls remain under Advanced storage settings.

### Managed S3 hierarchy and keys

App-managed S3 writes use stable server-owned IDs only:

```text
{managed-prefix}/
  workspaces/{workspace-uuid}/
    projects/{project-uuid}/
      source/{transfer-uuid}/object
      generated/{transfer-uuid}/object
    unassigned/{asset-or-transfer-uuid}/object
    probes/{cryptographically-random-probe-uuid}
```

Slugs and filenames are display metadata, not authority-bearing key segments. The canonical key builder rejects empty, absolute, dot, and traversal segments and proves the final key is a segment-boundary descendant of both the non-empty managed prefix and the source’s allowed prefixes.

Writes are private and create-only. They never send public ACLs, mutate object ACLs, or modify bucket policy/public-access configuration. A provider that cannot enforce create-only conditional writes is read-only for this release.

### Durable transfer intent

Before external I/O, Titles persists a transfer intent containing operation ID, job ID, asset or output subject, source location/provider, destination provider/source, canonical destination key, expected size when known, SHA-256 when known, and state.

The transfer UUID produces the immutable destination key. On retry, the worker reloads the intent, heads the exact key, and reuses it only when source identity, size, and stored SHA-256 metadata match. S3 ETag is not treated as a universal content hash. After verification, one SQLite transaction records the destination location, updates the asset preference when applicable, and completes the transfer intent. Unregistered temporary multipart uploads are aborted; cleanup may delete only app-owned temporary/probe objects tied to that operation.

### Browser-consumable delivery protocol

A shared web image component requests a same-origin delivery descriptor before setting `<img src>`:

```text
provider, delivery URL, fallback used, alternate provider, content revision
```

- Local delivery returns a stable same-origin content URL.
- S3 delivery returns a short-lived presigned GET URL.
- A pre-response provider failure returns a descriptor for a verified alternate and marks `fallback_used`.
- A later image-load failure requests the alternate provider once; retry loops are prohibited.
- Gallery and filmstrip fallback may remain visually quiet but expose accessible status.
- The full viewer shows `Showing from This computer because <source name> was unavailable` with `View storage details`.
- When no location works, the shared component renders an inline unavailable state with Retry and source context rather than a broken image.

Persisted asset metadata describes preference and locations only. Request-specific fallback lives in the delivery descriptor, component state, response headers, and redacted activity—not as a persisted asset flag.

### Thumbnail policy

Thumbnails are evictable derivatives, not authoritative copies. A remote original may be streamed through bounded temporary storage; only the resulting WebP remains in the local thumbnail cache. Cache identity includes asset ID, requested size, and authoritative content revision (SHA-256 or source-scoped ETag/version). Temporary originals are always removed after atomic thumbnail publication.

## Operator Flows

### Connect writable S3 storage

1. `Connect S3 storage` focuses the advanced source form.
2. Operator supplies friendly name, HTTPS endpoint or AWS default, bucket, read prefixes, managed write prefix, region/addressing, and credential-environment prefix.
3. Server validates URL and prefix policy before saving.
4. `Verify for image storage` creates a cryptographically random create-only probe below the reserved probe subtree.
5. The probe proves Put, Head/Get, presigned GET, SHA-256/body identity, and deletion of that exact object/version in a `finally` cleanup path.
6. Success records `Write ready` and last-verified time, then offers `Use <source name> for new images`.
7. Failure reports the failed capability in plain language, stores no secret/signed URL, leaves the source import-only, and offers Retry after configuration repair.
8. Capability status expires after 15 minutes for new writes and is invalidated immediately by active-state or credential-health failure. A worker revalidates the frozen source before starting an external write.

### Change the default for future images

1. Operator chooses S3 or Local.
2. S3 requires an explicitly selected currently writable account.
3. Operator chooses `New images only`.
4. The preference is saved.
5. Existing asset preferences and locations remain unchanged.
6. New jobs snapshot and use the selected policy.

### Change the default and copy existing images

1. Settings calculates eligible count, known bytes, unknown-size count, and exclusions by reason.
2. Operator chooses `New and existing images`; the prompt names the destination and says existing copies stay in place.
3. Settings saves the default and queues one durable migration job.
4. A persistent Settings status card links to migration detail; migration also appears in Transfers history and Activity.
5. Detail shows destination, state, completed/total assets, known bytes, copied/reused/skipped/failed/canceled counts, plain-language per-asset reasons, Cancel, and Retry failed items.
6. Retry preserves the original asset scope and frozen destination even if Settings changed later.
7. A second overlapping bulk migration is blocked until the active one completes or is canceled.
8. Successfully verified assets change preference; failed/canceled assets retain prior preference; all source copies remain.

### Import to S3

1. Both existing import entry points route to one canonical import preview component; the compact Transfers form becomes a shortcut into that flow.
2. The collapsed summary says `Storage: Use default — <provider and source name>` before expansion.
3. Advanced override offers Local or S3.
4. S3 keeps images at the selected import origin, even when the writable Settings destination is different.
5. Preview names the origin source and says images will remain there.
6. Job persists `origin_source_id`, `resolved_provider=s3`, and no write destination because no new object is written.
7. Imported locations bind to the origin source and retain original keys.

### Import to Local

1. Canonical preview names the S3 origin and says images will be copied to this computer.
2. Job persists origin source and `resolved_provider=local`.
3. Metadata is indexed; eligible image originals are atomically streamed and SHA-256 verified into managed local storage.
4. Remote origin remains recorded; verified local location becomes preferred.
5. Partial copy failure is visible in the import/migration result and remains retryable without duplicating verified copies.

### Persist FAL output

1. Policy is resolved and frozen when the FAL job is first enqueued; retries reuse it.
2. Provider result URLs must be HTTPS and match an adapter-owned artifact-host allowlist. Every redirect is revalidated; no credentials are forwarded.
3. Responses are streamed with backpressure, require an allowed image MIME type, are limited to 100 MiB, and pass safe image decoding/dimension limits.
4. Local mode commits a verified local location.
5. S3 mode creates a durable transfer intent and writes under the project `generated/` hierarchy.
6. If the frozen S3 destination remains unavailable after bounded retries, the result falls back to Local, records the actual durable provider and fallback reason, and keeps the desired provider visible in the job result.
7. Asset, image metadata, eval output, lineage, and location commit only after one durable verified result exists.

### Repair an unbound legacy source

1. Inspector shows `Source repair required` and no remote delivery action.
2. `Repair source` lists only same-workspace candidate sources matching retained bucket/prefix evidence.
3. Operator selects a source; server performs a bounded Head verification against stored key, size, and ETag/version when present.
4. Success binds the location, records actor/time/source, recalculates availability, and shows the source name.
5. Mismatch or network failure leaves it unbound and displays a redacted actionable reason.

## Inspector Contract

Primary summary:

- `Preferred storage: Mega S4` or `This computer`
- Current availability or request delivery state
- `Origin: <friendly source name>`

`Copies and availability` disclosure:

- one human-readable row per Local/S3 copy;
- source/account display name;
- Available, Missing, Verification required, or Source repair required;
- fallback eligibility.

`Technical details` disclosure:

- root-relative opaque local display path, never an absolute URI;
- bucket/key;
- hydration state;
- ETag/version;
- source UUID.

After successful request fallback, only the viewer/render state reports it. Persisted asset metadata continues to report preference, origin, copies, and availability.

## Security and Resource Boundaries

### Source and network boundaries

- Writable endpoints must use HTTPS, with no userinfo, query, or fragment.
- DNS resolution rejects loopback, link-local, private, multicast, and reserved targets and rechecks after redirects/reconnects. A separate explicit test-only mode may allow a local S3 emulator.
- S3/FAL requests use 10-second connect and 120-second read timeouts, at most three bounded retries, and no unbounded response buffering.
- Existing no-auth operation is supported only on loopback. Startup refuses non-loopback no-auth binding for source, settings, migration, and write APIs. CORS is not authorization.

### Local path boundary

All local reads and writes—serving, thumbnailing, migration, verification, and cleanup—use one canonical managed-root validator. New local locations store root-relative identities. Legacy paths are symlink-resolved and must remain under configured asset/cache roots. External absolute paths, traversal, and symlink escapes are ineligible and can never be uploaded.

### Signed URLs and redaction

- Browser image presigns are GET-only, default to 5 minutes, and are capped at 15 minutes.
- FAL checkpoint handoff uses a separate internal maximum of 24 hours because provider queue latency differs from interactive image delivery.
- Delivery descriptors and same-origin responses use `Cache-Control: private, no-store`; image elements use `Referrer-Policy: no-referrer`, and signed response parameters request private/no-store behavior where the provider supports them.
- A shared redactor applies before every job payload/result/error, activity detail, provider response, exception log, and HTTP access log. It removes signed query strings, Authorization headers, credential values, endpoint userinfo, and secret-like fields.

### Managed write limits

Advanced defaults:

- 2 GiB maximum imported/migrated image object;
- 100 MiB maximum FAL output;
- 50,000 assets per migration job;
- four concurrent transfers per job;
- three bounded retries per object.

Larger migrations are split into additional jobs. Multipart uploads are aborted on cancellation/failure. Cleanup can target only operation-owned temporary/probe objects, never verified source or destination copies.

## Requirements

### R1. Global default

Persist Local or S3 as the installation-global image-storage preference for the current single workspace. Auto-select S3 only with exactly one writable source; require explicit account selection when several exist.

### R2. Writable-source capability

S3 writes require a recent successful create-only Put, Head/Get, presigned GET, body/SHA verification, and exact-object Delete probe under the canonical managed/probe prefix. Capability status is distinct from read-only import browsing.

### R3. Canonical import experience

Both current import entry points must use one preview and one policy contract. The routine summary exposes inherited provider/source in plain language; Local/S3 override remains advanced.

### R4. Operation coverage

Imported images and FAL outputs honor the frozen policy. Future upload implementations must consume this policy, but a new upload UI is not part of this release. Checkpoint hydration remains explicit.

### R5. Source-scoped identity

Every new S3 location has a non-null owning source; uniqueness, reconciliation, hydration, delivery, migration, and FAL resolution use source plus object key. Workspace/project ownership is checked at every boundary.

### R6. Immutable referenced source identity

Connection identity and prefix policy cannot mutate after a location or transfer references a source. Replacement source records carry identity changes; secret values rotate behind stable environment names.

### R7. Conservative legacy handling

Unbound legacy S3 locations are metadata-only and ineligible for remote operations until explicit verified repair. Existing valid local locations remain Local preferred during rollout.

### R8. Source-aware metadata

API and inspector expose preferred provider, resolvable provider, friendly origin source, all copies/states, and fallback availability. Raw paths are bounded/opaque; technical fields are disclosed only in the technical section.

### R9. Delivery descriptor

The web app resolves images through a same-origin descriptor that reports resolved provider and request-specific fallback before setting image source. One alternate-provider retry is allowed after load failure; preference never mutates.

### R10. Remote delivery

Bound S3 images use on-demand short-lived GET-only presigned URLs. URLs are never persisted or logged and use the defined cache/referrer protections.

### R11. Local boundary

Every local read/write validates canonical managed-root containment after symlink resolution. Arbitrary or external local URIs cannot become migration sources.

### R12. Versioned thumbnail cache

Only WebP derivatives persist locally for remote originals. Cache identity includes authoritative content revision; transient originals are removed.

### R13. Safe managed keys and writes

Canonical keys use server-owned IDs, remain under managed and allowed prefixes, are private, and are created conditionally without overwrite. Unsupported providers remain read-only.

### R14. Durable transfer intent

External writes occur only after a durable transfer intent exists. Retry reuses only an exact verified destination; SHA-256—not ETag alone—establishes content identity.

### R15. Copy preservation

Migration copies and verifies, switches preference only after success, and never deletes prior physical locations. Partial failure and cancellation preserve source and prior preference.

### R16. Migration recovery UX

Settings status, Transfers history, and migration detail expose progress, cancellation, results, per-asset redacted reasons, and retry with frozen scope/destination.

### R17. FAL retrieval safety

FAL artifact fetches use adapter-owned HTTPS host allowlists, redirect revalidation, MIME/size/decode limits, timeouts, bounded retries, and no credential forwarding.

### R18. Private storage

The implementation must not send public ACLs, generate public object URLs, or change bucket/object public-access policy. Credentials remain environment-only.

### R19. Loopback trust boundary

Without authentication, privileged source/settings/migration/write APIs operate only on loopback; non-loopback startup is refused.

### R20. Redaction

Signed URLs, credentials, authorization data, and endpoint userinfo are removed from all persisted state, activity, errors, provider payloads, and logs.

### R21. Resource bounds

Transfers stream with backpressure and enforce object count, byte, concurrency, retry, timeout, multipart-abort, and operation-owned cleanup limits.

### R22. Observability

Jobs and activity expose friendly provider/source, frozen desired provider, actual durable provider, counts/bytes, fallback, and redacted per-asset failures.

## Acceptance Examples

### AE1. One writable source at first use

Given exactly one verified writable S3 source and no saved preference, Settings selects S3 and that named source; the first job persists that source as its write destination.

### AE2. Several writable sources at first use

Given two writable sources and no saved destination, Local remains effective and no S3 write can start until the operator explicitly chooses one named account.

### AE3. Connect and verify storage

Given no writable source, Connect storage accepts a valid public HTTPS S3 configuration, runs the random create/read/presign/delete probe, cleans the probe, marks the source Write ready, and offers to use it. A failed probe leaves S3 disabled and offers Retry without exposing secrets.

### AE4. Future writes only

Given existing Local-preferred images, switching to S3 and choosing New images only creates no migration job and leaves existing preferences unchanged.

### AE5. Copy existing images

Choosing New and existing images creates one migration with frozen destination, a persistent status card, Transfers row, copied/reused/skipped/failed/canceled counts, and retry. Verified assets switch preference; sources remain.

### AE6. Cross-source S3 import

Given writable destination A and import origin B, S3 import preview says images remain in B; job records B as origin, writes no object to A, and serves through B.

### AE7. Local import override

Given global S3 preference, a Local override copies and verifies local originals while preserving S3 origin; later inherited imports remain S3.

### AE8. Remote display

Given a bound S3-preferred image with no local original, the delivery descriptor returns S3 and a short-lived URL; the image renders and no durable local original is created.

### AE9. Request fallback

Given S3 preference plus verified Local copy, S3 failure produces one Local retry, viewer notice, and accessible gallery status; persisted preference remains S3.

### AE10. No available copy

When all verified locations fail, the shared image component renders Retry and source context instead of a broken image or retry loop.

### AE11. FAL output in S3 mode

A valid FAL artifact is streamed under limits into the frozen project destination and committed only after verification. A failed destination falls back to Local and records desired versus actual provider.

### AE12. Migration crash and retry

If the worker crashes after upload but before SQLite location commit, retry reloads the transfer intent, verifies and reuses the exact immutable object, creates one location, and does not duplicate bytes.

### AE13. Ambiguous legacy repair

An unbound legacy location is unavailable for remote delivery. Selecting a same-workspace candidate binds it only after Head verification; mismatch leaves it unbound and records no guessed source.

### AE14. Duplicate bucket/key isolation

Two sources on different endpoints with identical bucket/key never reconcile, presign, hydrate, or migrate through one another’s credentials.

### AE15. Managed-key safety

Malicious slugs, filenames, traversal strings, external local paths, and symlink escapes cannot influence an authorized managed key or become upload sources.

### AE16. Signed URL redaction

Injecting a signed URL into a provider error produces no signed query in jobs, activity, API errors, database rows, or captured logs.

### AE17. Loopback enforcement

A no-auth attempt to bind privileged storage APIs to a non-loopback interface fails startup with an actionable message.

### AE18. Rename stability

Renaming a workspace/project leaves UUID-based object keys resolvable without moves.

## Test Strategy

- Alembic upgrade tests start from revision `0002_generic_s3_sources` and verify source FK, preference, transfer-intent schema, replacement uniqueness, Local-preference backfill, and unbound legacy state.
- API contract tests cover effective default resolution, multi-account selection, source immutability, source repair, metadata hierarchy, delivery descriptors, fallback hints, signed-url TTL/header/redaction, loopback enforcement, and local/S3 boundaries.
- S3 integration tests cover duplicate bucket/key isolation, canonical prefix validation, conditional create, exact-object probe cleanup, capability expiry, upload/head/get/delete, presigned delivery, and unsupported-write providers.
- Import tests cover the canonical preview in both entry points, source B versus destination A, inherited/overridden policy snapshots, local materialization, partial results, and retry.
- Worker tests cover transfer-intent crash windows, idempotent reuse, checksum mismatch, cancellation, multipart abort, resource limits, migration progress/results, and overlapping-migration prevention.
- FAL tests cover frozen enqueue policy, allowed/rejected hosts, redirect revalidation, MIME/size limits, no-length streaming, S3 persistence, Local fallback, and transaction/orphan cleanup.
- Add a focused React test runner before feature UI work. Component/browser tests cover primary Settings simplicity, disabled/repaired S3 states, multi-source choice, migration prompt/card/detail, import shortcut/preview, inspector disclosures, one-time fallback, accessible status, and unavailable-image retry.
- The live MEGA S4 smoke test is opt-in and outside ordinary CI. It uses a random probe key below the managed probe prefix, verifies conditional write/head/presigned GET/SHA/delete, cleans only that exact probe in `finally`, and asserts no signed query is captured in logs or persistence.

## Rollout

1. Add schema/source identity, transfer-intent tables, and conservative backfill; keep current Local serving unchanged.
2. Add source repair, source-scoped queries, canonical path/key validators, redaction, and loopback enforcement.
3. Add capability probing and delivery-descriptor contracts behind tests; retain Local compatibility.
4. Enable remote content and remote-thumbnail derivation.
5. Enable frozen policy for imports and FAL outputs.
6. Add simplified Settings, canonical import preview, migration prompt/status/detail, and Transfers integration.
7. Enable bulk migration only after idempotence, crash-window, cancellation, and restart tests pass.
