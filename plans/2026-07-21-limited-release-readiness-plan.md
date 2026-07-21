# Limited Release Readiness Plan

## Goal

Prepare Modelfiche for a one-company, one-user release in which the user and their agent operate the application, S3, and external training infrastructure. Make installation, recovery, health, and agent-driven workflows dependable without turning Modelfiche into a credential manager or RunPod control plane.

## Explicit non-goals

- Developer ID signing, notarization, or Gatekeeper approval.
- Storing, brokering, or managing S3, FAL, LLM, RunPod, or tunnel credentials. Credentials remain agent-managed environment inputs.
- Provisioning, starting, stopping, or billing RunPod resources.
- Shipping or maintaining a RunPod template.

The unsigned release must still publish checksums and concise installation instructions that state the required macOS security override.

## Release priorities

### P0 — Required before the company pilot

#### 1. Simplify training connectivity

Replace the required inbound W&B tunnel with an outbound-only S3 synchronization path where practical. Modelfiche writes launch state to S3; the agent-managed trainer reads it and publishes metrics, samples, logs, and checkpoints back to S3; Modelfiche polls and reconciles them.

If live ingress remains available, make it optional and retain the current signed run-scoped authentication. Do not add tunnel credentials or RunPod lifecycle management.

**Acceptance**

- A training run can complete without exposing the user's Mac to inbound internet traffic.
- Restarting Modelfiche or losing connectivity does not lose trainer events or checkpoint state.
- The UI distinguishes delayed S3 synchronization from live ingress.
- Existing live-ingress users remain supported or receive an explicit migration path.

#### 2. Complete agent CLI parity

Give every supported user mutation a JSON-capable CLI operation. Close the known gaps: single-image generation, asset deletion, caption word removal, grid cancellation, checkpoint verification, lineage-path traversal, and local-folder import. Audit remaining UI actions against API and CLI coverage.

**Acceptance**

- Every supported mutating UI action has a documented `mfiche --json` equivalent or is explicitly classified as UI-only with a product reason.
- Preview, preflight, confirmation, and error contracts match between UI and CLI.
- Unsupported placeholder commands are removed or implemented; no command advertises unavailable behavior.

#### 3. Add backup, verification, restore, and upgrade rollback

Create operator workflows for the SQLite database, local assets, settings, and non-secret configuration references. Automatically make a verified backup before schema upgrades. Never export secret values.

**Acceptance**

- `mfiche backup create`, `verify`, `list`, and `restore` work on an installed data directory.
- Restore refuses incompatible or corrupt archives without changing live data.
- A failed upgrade can restore the last verified pre-upgrade state.
- The UI shows backup location, timestamp, application version, and verification status.

#### 4. Add authoritative worker health

Persist a dedicated worker heartbeat with worker version, start time, and last-seen time. Stop inferring worker health from job timestamps.

**Acceptance**

- API health reports API/database, worker, queue depth, oldest waiting job, storage, and synchronization freshness separately.
- The UI reports a stopped or stale worker before work is submitted and while jobs are waiting.
- Diagnostics provide a safe restart instruction and preserve queued work.

#### 5. Make the full release test gate reliable

Fix test isolation so a single backend command collects API, worker, CLI, compatibility, and integration tests without duplicate-module collisions.

**Acceptance**

- One documented backend command passes from a clean checkout.
- Frontend tests, typecheck, and production build remain part of the same release checklist.
- Release automation fails on collection errors, migration divergence, or incompatible W&B behavior.

#### 6. Prove the unsigned packaged release on a clean Mac

Test the DMG, embedded runtime, migrations, frontend serving, CLI, persistence, restart, and upgrade without relying on a development checkout. Signing remains out of scope.

**Acceptance**

- Published artifacts include SHA-256 checksums and version metadata.
- Installation instructions clearly describe the one-time macOS security override for the unsigned app.
- The app launches its API, worker, ingress/synchronizer, and frontend from the bundle.
- The embedded CLI uses the installed application data directory.
- An older supported database upgrades successfully after creating a verified backup.

### P1 — Required before sustained production use

#### 7. Add a first-run readiness surface

Provide one status page for local data and backups, configured storage, generation providers, training connectivity, worker health, and CLI availability. Report credential presence and connection results without accepting or rendering credential values.

**Acceptance**

- Each blocked check has one actionable agent-oriented command or configuration instruction.
- Configuration presence, network verification, and recent operational success are distinct states.
- Training and generation remain disabled when their required checks fail.

#### 8. Add one-step CLI installation guidance

Provide an in-app copyable command or installer action that exposes the embedded `mfiche` executable on the user's `PATH`. Do not require repeated use of the application-bundle path.

**Acceptance**

- The readiness page can confirm which `mfiche` executable will run.
- Installation is user-scoped and does not require modifying the application bundle.
- The full bundle path remains documented as the fallback.

#### 9. Split and simplify Settings

Separate General, Storage, Generation, Training connectivity, Profiles, and Diagnostics. Keep advanced endpoint, addressing-style, environment-prefix, and ingress fields behind advanced sections.

**Acceptance**

- Common settings no longer require scanning one long mixed-purpose page.
- Storage and training checks are visible beside the configuration they validate.
- Technical labels include plain-language descriptions and copyable agent instructions.

#### 10. Make failed work recoverable

Replace raw-error dead ends with a human summary, preserved-work statement, safe Retry when supported, diagnostics disclosure, and linked activity history.

**Acceptance**

- Import, export, generation, synchronization, and checkpoint failures identify what failed and what remains intact.
- Retry re-runs preflight and cannot duplicate completed work silently.
- Raw provider errors and request IDs remain available under diagnostics.

#### 11. Exercise interruption and recovery scenarios

Add release scenarios for interrupted import, interrupted generation polling, trainer/S3 synchronization restart, expired agent-managed credentials, worker restart, Mac restart, and failed schema upgrade.

**Acceptance**

- Each scenario has a deterministic recovery procedure.
- Durable jobs resume or enter a terminal actionable state.
- No scenario requires direct database editing.

#### 12. Add a redacted support bundle

Export application version, component health, configuration shape, job history, recent logs, migration state, and relevant request IDs while excluding credentials, tokens, signed launch secrets, and sensitive prompt/image content by default.

**Acceptance**

- Automated tests assert secret redaction.
- The user can inspect the archive manifest before sharing it.
- Bundle creation works even when the worker is unavailable.

### P2 — Usability and operational polish

#### 13. Improve operational labels

Lead with human names and states in Queue, Transfers, Runs, Models, and Gallery. Keep UUIDs, provider codes, and raw status values as secondary copyable details.

#### 14. Add cost and consequence review

Before FAL generation, grid submission, exports, and other expensive or privacy-relevant work, summarize provider, request count, destination, estimated size, and known cost boundaries. RunPod billing remains outside Modelfiche; training screens should state that the agent controls external compute.

#### 15. Add update and rollback UX

Detect available application versions, show migration implications, require a verified backup, and retain a documented rollback path. Automatic self-update is not required.

#### 16. Continue responsive and large-review hardening

Optimize Queue, Grid, Eval, Samples, Gallery, and Dataset for bounded rendering, keyboard traversal, and narrow screens. Preserve vertical page scrolling and context-aware return paths.

#### 17. Reduce frontend delivery weight

Split large route-level bundles and lazy-load review workspaces without delaying the initial dashboard or hiding loading/error states.

## Release gate

The pilot is ready when this scenario succeeds on a clean Mac without developer intervention:

1. Install the unsigned DMG using the documented security override and verify its checksum.
2. Launch Modelfiche and install or expose the `mfiche` CLI.
3. The user's agent supplies required credentials through documented environment inputs.
4. Configure and verify S3 without storing credentials in Modelfiche.
5. Import, inspect, edit, publish, and export a dataset.
6. Prepare a training launch from an immutable dataset version.
7. The user's agent starts external training; Modelfiche receives metrics, samples, and checkpoints through the supported connectivity path.
8. Verify and review a returned checkpoint, run an evaluation, and record a production decision.
9. Restart the worker and Mac and confirm durable state recovery.
10. Create and verify a backup, perform an upgrade, and prove rollback from the pre-upgrade backup.
11. Repeat the supported mutations through `mfiche --json` without needing an undocumented API call.

## Release evidence

For each candidate release, retain:

- Backend, CLI, compatibility, integration, frontend, typecheck, and production-build results.
- Clean-install and upgrade/rollback results.
- End-to-end S3 import and training synchronization evidence.
- Backup/restore verification receipt.
- Packaged-app health and worker-heartbeat output.
- Artifact checksums and known limitations, including unsigned macOS distribution, agent-managed credentials, and externally managed RunPod compute.
