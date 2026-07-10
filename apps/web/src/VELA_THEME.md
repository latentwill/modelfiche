# Vela application theme

The approved dashboard direction is implemented across the production application:

- `vela-theme.css` — scoped tokens, shell, navigation, cards, runs, chart, inspector, drawers, focused viewers, and responsive rules.
- `shell.tsx`, `ui.tsx`, and the `screens-*.tsx` modules — live API-backed composition and interaction states.

The production root imports the theme after the legacy wireframe stylesheet and wraps every route in `.vela-theme`.

## Production contract

The shell owns presentation state for open/closed rails, responsive drawers, create-menu visibility, global search, profile selection, metadata, notes, and activity. Screen modules own API data, mutations, filtering, and task-specific state.

## Implementation

The CSS remains scoped beneath `.vela-theme` while legacy domain selectors are mapped to Vela tokens.

1. `App.tsx` provides the theme scope and route boundaries.
2. `shell.tsx` implements the three-column desktop shell and responsive rail drawers.
3. `ui.tsx` provides shared pages, panels, forms, notices, tables, profiles, and status states.
4. `screens-wireframe.tsx` implements the live dashboard template semantics and focused review workspaces.
5. Domain screen modules preserve wireframe workflows while using the shared Vela component contract.

## Existing app mapping

| Current app | Vela package |
| --- | --- |
| `.wireframe-shell` | `.vela-shell` |
| `.left-sidebar` | `.vela-sidebar` |
| `.right-inspector` | `.vela-inspector` |
| `.topbar` | `.vela-topbar` |
| `.project-card-grid` | `.vela-project-grid` |
| `.project-card` | `.vela-project-card` |
| `.dashboard-lower` | `.vela-dashboard-grid` |
| `.run-list` | `.vela-run-list` |
| `.inspector-panel` | `.vela-inspector-card` |

## Token policy

- `--vela-accent` is reserved for primary creation, active navigation, selected projects, links, and data emphasis.
- `--vela-surface-soft` provides the only secondary neutral layer.
- `Vela Display` is used for headings and compact editorial labels; Avenir Next remains the body and control face.
- Mono is limited to identifiers, dates, compact metadata, and aligned numerics.
- Rail widths, topbar height, radii, motion, and spacing are tokens rather than component-local values.

## Data contracts

`VelaProject.metrics` supplies the four compact values used by both the project card and metadata inspector. Preserve label order intentionally; the template displays the first four entries.

`VelaProject.modelNames`, `datasetNames`, and `evalNames` populate the expanded selected-project tree. Omit any empty group instead of rendering a placeholder.

`VelaCreateAction` keeps the create menu application-owned. Pass real actions for project, dataset, model, eval, grid, and import workflows; the template never performs mutations itself.

## Accessibility and responsive behavior

- Project selection uses `aria-pressed` and a full-card focus ring.
- Both rails expose explicit collapse and reopen buttons with `aria-controls` and synchronized expansion state.
- At 1180px the metadata inspector is removed from the layout; at 680px navigation becomes compact mobile chrome.
- Motion is disabled under `prefers-reduced-motion`.
- The stylesheet guards against page-level horizontal overflow through `minmax(0, 1fr)` shell columns and responsive single-column fallbacks.
