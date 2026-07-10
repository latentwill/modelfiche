# Wireframe Implementation Audit

Audit source: the 14 root HTML wireframes. The React application follows their
information architecture; backend coverage was checked in both directions.

## Shared Shell

| Contract | Status | Implementation |
| --- | --- | --- |
| Workspace switcher and breadcrumbs | Implemented | Persistent top bar |
| Global cross-object search | Implemented | `/api/search` result popover |
| Import, Export, and global create menu | Implemented | Persistent top actions |
| Main: Dashboard and Gallery | Implemented | Canonical left navigation |
| Dynamic Projects tree | Implemented | Models, Datasets, and Evals fetched per project |
| Operations: Transfers | Implemented | Durable import/export history |
| Settings and storage summary | Implemented | Bottom navigation block |
| Collapsible left and right rails | Implemented | Persisted locally |
| Context metadata, notes, and activity | Implemented | Entity-scoped right inspector |

## Screen Coverage

| Wireframe | React route | Backend contract | Status |
| --- | --- | --- | --- |
| Dashboard | `#/dashboard` | projects, runs, activity, dashboard/search | Implemented |
| Project workbench | `#/project/:id` | project aggregates, artifacts, activity, notes | Implemented |
| Dataset editor | `#/dataset/:id` | versions, drafts, scoped caption operations | Implemented |
| Training run | `#/run/:id` | checkpoints, samples, config, lineage, refresh | Implemented |
| Model artifact | `#/model/:id` | versions, evals, training runs, lifecycle | Implemented |
| Eval generator | `#/evals?project=:id` | endpoint schemas, prompt sets, eval jobs | Implemented |
| Eval viewer | `#/eval/:id` | outputs, ratings, comments | Implemented as dedicated viewer |
| Grid generator | `#/grids?project=:id` | endpoint axes, definitions, durable grids | Implemented |
| Grid lightbox | `#/grid/:id` | composed cells and output assets | Implemented as dedicated viewer |
| Gallery | `#/gallery` | filters, search, asset thumbnails | Implemented |
| Image lightbox | `#/image/:id` | context, content, ratings, comments | Implemented as dedicated viewer |
| Sample viewer | `#/samples/:run-id` | step-grouped samples and reviews | Implemented as dedicated viewer |
| Settings | `#/settings` | nonsecret settings and provider tests | Implemented |
| Transfers | `#/transfers` | imports, durable ZIP exports, download/cancel | Implemented |

## Reverse-Orphan Audit

Previously unused backend functions now have UI affordances for notes, lineage,
raw run configuration, checkpoint hydration, job logs/cancel, model approval and
archive, prompt-set versioning, endpoint-derived schemas, asset context, provider
tests, gallery review filters, import cancellation, and durable exports.

Some API routes remain intentionally CLI/system surfaces rather than primary UI:
system enums/version/capabilities, low-level asset location CRUD, direct raw job
creation, and compatibility aliases. Run `pnpm --dir apps/web audit:contracts`
against a running API to detect frontend paths that do not match OpenAPI.

## Explicit Boundaries

- Authentication and hosted client handoff remain deferred by product decision.
- Provider credentials remain environment-only and are never stored or returned.
- Metrics and EXIF are displayed only when imported source artifacts contain them;
  the UI shows an explicit unavailable state instead of fabricated values.
- Client-specific export permissions and hosted share links are not part of the
  local operator release. Local review ZIP packages are operational.
