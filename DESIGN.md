---
name: ModelFiche Web
description: Cobalt archival workbench for inspecting, comparing, and reviewing visual model-training assets.
colors:
  light-canvas: "#fbfbf8"
  light-blue-wash: "#f2f4fa"
  light-paper: "#ffffff"
  cobalt: "#1237b8"
  cobalt-deep: "#082689"
  metadata-blue: "#6573a4"
  blue-border: "#b8c5ed"
  archive-green: "#176b4a"
  archive-amber: "#8b580e"
  archive-red: "#a12b32"
  media-night: "#05104a"
  observatory-blue: "#081b78"
  observatory-ink: "#f7f5ec"
  observatory-muted: "#bfc9f4"
  observatory-border: "#8fa5ef"
  observatory-accent: "#87a9ff"
  observatory-success: "#8ed8b3"
  observatory-warning: "#f0c675"
  observatory-danger: "#ff9aa0"
typography:
  display:
    fontFamily: '"Vela Display", "HelveticaNeue-CondensedBold", "Helvetica Neue Condensed Bold", "Arial Narrow", sans-serif'
    fontSize: "42px"
    fontWeight: 700
    lineHeight: 0.94
    letterSpacing: "-0.035em"
  body:
    fontFamily: '"Avenir Next Condensed", "Avenir Next", "Helvetica Neue", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  support:
    fontFamily: '"Avenir Next Condensed", "Avenir Next", "Helvetica Neue", sans-serif'
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.45
  section:
    fontFamily: '"HelveticaNeue-CondensedBold", "Helvetica Neue Condensed Bold", "Arial Narrow", sans-serif'
    fontSize: "0.9375rem"
    fontWeight: 700
    lineHeight: 1.2
  wordmark:
    fontFamily: '"Iowan Old Style", Charter, Georgia, serif'
  mono:
    fontFamily: '"SFMono-Regular", "Roboto Mono", Menlo, monospace'
    fontSize: "10px"
rounded:
  square: "0"
  sm: "2px"
  md: "4px"
spacing:
  1: "4px"
  2: "8px"
  3: "12px"
  4: "16px"
  5: "24px"
  6: "32px"
components:
  button-primary:
    backgroundColor: "{colors.cobalt}"
    textColor: "{colors.light-paper}"
    rounded: "{rounded.square}"
    padding: "8px 13px"
    height: "38px"
  button-secondary:
    backgroundColor: "{colors.light-paper}"
    textColor: "{colors.cobalt-deep}"
    rounded: "{rounded.square}"
    padding: "8px 13px"
    height: "38px"
  text-input:
    backgroundColor: "{colors.light-paper}"
    textColor: "{colors.cobalt-deep}"
    rounded: "{rounded.square}"
    padding: "9px 11px"
    height: "40px"
  navigation-active:
    backgroundColor: "{colors.cobalt}"
    textColor: "{colors.light-paper}"
    rounded: "{rounded.sm}"
    padding: "9px 10px"
    height: "40px"
  panel:
    backgroundColor: "{colors.light-paper}"
    rounded: "{rounded.md}"
    padding: "16px"
---

## Overview

**Creative North Star: "The Cobalt Xerox Archive"**

ModelFiche is a serious, precise, production-minded archive workbench. Its interface borrows the legibility of a cobalt photocopy, catalog sheet, and technical observation log: paper-light or observatory-dark grounds; sharply ruled blue structure; compact numbered labels; and dense information that stays navigable. Visual assets remain the focal material. The UI makes their lineage, status, comparison, and review state explicit without becoming a generic SaaS dashboard.

The app has two intentional application themes: a light **cobalt xerox sheet** and a dark **observatory**. The choice is an operator preference, persisted in local storage and initialized from the OS preference when no saved choice exists. It is not a decorative inverse palette. Each theme has its own semantic surface, ink, border, accent, state, grid, media-bed, logo, and reader-symbol variants.

The desktop shell preserves work context with persistent navigation, a task surface, and a metadata/activity rail. At the same time, the content region is a responsive 12 / 8 / 4 column grid, and dedicated image/grid review workspaces may leave the three-rail shell when visual inspection requires more canvas. Generated visual reference assets live in `apps/web/public/assets/`; the canonical live system is the ModelFiche semantic-token layer.

**Key Characteristics:**
- Cobalt-blue archival identity with paper-light and observatory-dark modes.
- Squared geometry, double rules, technical grid lines, and small offset shadows—not soft consumer cards.
- Condensed display hierarchy, condensed body copy, serif wordmark, and mono metadata.
- Images are treated as inspectable specimens: contained when fidelity matters, cover-cropped only for fast scanning.
- Context-preserving rails, local scrolling surfaces, responsive drawers, and no horizontal page overflow.

**The Identity-Over-Implementation Rule.** ModelFiche is the design-system name. An inherited CSS compatibility namespace exists in source, but it is not a product, theme, component, or typography name for new work.

## Colors

The frontmatter values are the normative palette. Use the semantic ModelFiche roles they represent; do not introduce untracked blues, grayscale ramps, or arbitrary dark-mode overrides.

### Primary

- **Cobalt Ink** (`cobalt`): primary action fill, current navigation, selection, data traces, input focus stripe, and structural emphasis in light mode.
- **Cobalt Deep** (`cobalt-deep`): strongest light-mode text and the dark edge of the archival palette.
- **Observatory Accent** (`observatory-accent`): dark-mode primary action, selected state, and emphasis. It replaces—not merely lightens—Cobalt Ink when the dark theme is active.

### Semantic State

- **Archive Green / Observatory Success** (`archive-green`, `observatory-success`): ready, healthy, complete, approved, and active work.
- **Archive Amber / Observatory Warning** (`archive-amber`, `observatory-warning`): queued, pending, running, draft, held, attention, and unknown work.
- **Archive Red / Observatory Danger** (`archive-red`, `observatory-danger`): failed, rejected, missing, cancelled, and error states.

### Neutral

- **Xerox Canvas** (`light-canvas`), **Blue Wash** (`light-blue-wash`), and **Archive Paper** (`light-paper`) establish the light-mode background, soft fill, and working surface.
- **Metadata Blue** (`metadata-blue`) and **Blue Border** (`blue-border`) make secondary facts and one-pixel structure legible without weakening the cobalt hierarchy.
- **Observatory Blue** (`observatory-blue`), **Observatory Ink** (`observatory-ink`), **Observatory Muted** (`observatory-muted`), and **Observatory Border** (`observatory-border`) replace the light roles in dark mode.
- **Media Night** (`media-night`) is the image well for light-mode asset previews; it is a media surface, not a general-purpose dark panel.

### Theme and Asset Variants

- The root `data-theme` attribute owns the light/dark mode. The explicit topbar toggle exposes the inverse-mode action, is keyboard-focusable, and reports its pressed state.
- The runtime wordmark is `modelfiche-logo-light.png` or `modelfiche-logo-dark.png`; choose it from the active theme only. Reader art follows the same rule with `modelfiche-reader-symbol-light.png` and `modelfiche-reader-symbol-dark.png`.
- Additional public assets are generated visual-reference material: texture sheets, hero imagery, coordinate and ruler overlays, archive stamps, registration crops, microfiche readers, logo studies, and observatory fragments. Reuse them only when their specific visual role is needed; do not turn them into a generic decorative asset library.

**The Cobalt Allocation Rule.** Cobalt carries active state, decision, and structural hierarchy. It must not become background decoration on inactive cards, empty space, or every icon. In dark mode, use the observatory roles rather than forcing light-mode values onto a dark surface.

## Typography

**Display Font:** ModelFiche Display—the local `Vela Display` face with the condensed Helvetica Neue fallback stack defined by the production display role.

**Body Font:** Avenir Next Condensed with Avenir Next and Helvetica Neue fallbacks.

**Wordmark Font:** Iowan Old Style with Charter and Georgia fallbacks; use only for the supplied ModelFiche wordmark treatment.

**Label/Mono Font:** SF Mono / Roboto Mono / Menlo.

**Character:** The hierarchy should read like a technical catalog: forceful condensed titles, calm condensed body text, and monospaced labels that make identifiers and measurements easy to compare. The display face is ModelFiche's visual voice; the wordmark is an asset, not an invitation to set arbitrary prose in serif.

### Hierarchy

- **Hero title** (700, 42px desktop / 32px mobile, 0.94): uppercase dashboard statement; use only in the hero construction.
- **Page title** (700, 32px desktop / 28px mobile, 1.05): uppercase route title on the numbered overview sheet.
- **Section and card title** (700, 15–16px): condensed uppercase headings with tightly controlled negative tracking.
- **Body** (400, 13–14px, 1.45): operator-facing explanations; keep overview copy to the established 65–68ch bounds.
- **Technical label** (700, 8–10px, mono): uppercase nav groups, table headings, timestamps, chart labels, IDs, status, and generated registration marks. Preserve tabular numerics and the established tracking.

**The Catalog-Role Rule.** Display type names a thing; body type explains it; mono classifies and measures it. Do not use display type for editable values, table body data, or general button labels. Do not use serif except through the wordmark asset.

## Elevation

ModelFiche layers by rules, paper washes, grid textures, inset lines, and small hard offsets. Standard panels carry a thin border, a stronger top rule, and a subtle two-pixel structural offset. Menus use a four-pixel hard offset; dialogs use a twelve-pixel hard offset. These are printed-object registration cues, not diffuse product-card shadows. In dark mode, the shadow deepens while surfaces retain their explicit borders.

**The Registration Rule.** Depth must look constructed: a rule, double rule, inset line, or hard offset. Do not add rounded floating cards, ambient blur, or soft elevation to static work regions.

**The Motion Rule.** Use the existing 160ms exponential transition for rail changes and the two-pixel project-card hover translation. The DitherKit loss chart has an 1100ms data-draw animation; do not add page-load choreography. Disable nonessential movement under reduced-motion preferences.

## Components

### Buttons

**Character:** square, literal controls on an archival sheet.

- **Primary:** Cobalt Ink in light mode and Observatory Ink-on-accent pairing in dark mode. Use for the decisive creation, save, or selected tab action.
- **Secondary:** paper surface, explicit border, square corners, compact padding, and no default shadow.
- **Icon and theme controls:** squared compact controls. The theme toggle shows the inverse available mode and has a small lower-right registration marker; its pressed state identifies dark mode.
- **Focus and disabled:** focus is a thin strong-outline construction with a white separator and outer cobalt ring. Disabled controls reduce opacity and must remain visibly non-interactive.

### Inputs / Fields

**Character:** the numbered Entry Console / Input Sheet.

- Forms receive a double strong border and the `ENTRY CONSOLE / INPUT SHEET` heading treatment.
- Each field is sequentially numbered, with the two-digit number rendered in Cobalt Ink before its mono label.
- Inputs, selects, and textareas are opaque paper surfaces with square corners, a one-pixel border, and a stronger two-pixel lower rule. On focus, the left inset stripe becomes the accent; textarea entry expands vertically.
- Use the responsive form grid with a 190px minimum field width. On mobile, use one column and preserve the form sheet padding hierarchy.

### Sheets, Cards, and Tables

**Character:** catalog sheets and microfiche records, not generic card grids.

- Panels, inspector cards, and page headers use explicit blue rules, a very small corner radius, and paper backgrounds. Overview sheets add a 16px grid plus catalog marks; do not duplicate those marks in ordinary panels.
- Project cards use a strong inset top rule, paper/blue-wash segments, and a hard-offset hover response. Selection retains a full blue state treatment, never a soft glow.
- Tables use squared boundaries, a strong double-weight header rule, 12px cells, and alternating soft bands. Clickable rows add an inset cobalt stripe on hover.
- Notices use a full one-pixel semantic border and tinted surface; errors swap that semantic treatment to Archive Red. Empty states use a dashed structural boundary and a catalog glyph.

### Navigation and Shell

**Character:** persistent rails and ruler infrastructure.

- Desktop layout is 248px navigation, flexible workspace, and 312px inspector; either rail can collapse to a 48px vertical reopening label.
- Both rails are sticky, full-height surfaces with fine repeated ruler lines and strong cobalt boundaries. Navigation groups start with a cobalt section mark; the current route is a filled cobalt state.
- The 64px topbar contains breadcrumbs, search, creation, profile, and the theme switch. Menus and search results use explicit strong borders and a hard offset.
- The main workspace is 12 columns at desktop, 8 columns at 1180px, and 4 columns at 680px. At 1180px the inspector becomes a drawer; at 680px navigation becomes a drawer, touch controls grow to 44px, and the document body remains horizontally clipped.

### Image Cards and Visual Review

**Character:** inspectable visual specimens.

- Gallery asset cards use a square preview with a Media Night bed, a tiny corner radius, clipped metadata, and a cobalt selection/hover boundary. Use `object-fit: contain` where the entire specimen matters.
- Project mosaics, sample strips, evaluation strips, and chart-adjacent thumbnails may use `object-fit: cover` to support rapid scanning. Never crop an asset view whose primary job is detailed inspection.
- Focused review uses the light/dark theme's media-bed, a contained main image, an adjacent light/dark metadata inspector, and a filmstrip with a strong selected outline. It is a dedicated review workspace, not a modal substitute.

### Charts and Grids

**Character:** technical plotting planes with intentional machine texture.

- Loss charts use a clean paper or observatory plane, a strong bottom axis rule, semantic accent trace, muted technical labels, and DitherKit's area/grid/tooltip composition. The current chart reports the final loss in its mono kicker.
- Grid boards are square-cell matrices with local scrolling, four-pixel gaps, structural column labels, dark media cells, and a strong inset outline for the selected result.
- Keep plotting surfaces clear: no decorative gradients or unrelated image texture inside charts and grids. The dark observatory texture belongs to open workspace/title regions, not data planes.

## Do's and Don'ts

### Do:

- **Do** name the system, typography role, token vocabulary, and visual conventions **ModelFiche**. Treat inherited CSS compatibility names as implementation details only.
- **Do** use the active theme's semantic roles for every surface, text, state, border, media well, reader symbol, and logo variant.
- **Do** use cobalt rules, mono catalog labels, numbered form fields, and hard registration offsets to establish the archival visual language.
- **Do** preserve the three-rail context model for task work; use focused review workspaces for visual inspection that needs the canvas.
- **Do** use contained previews for fidelity and cover crops only for scan-oriented groups.
- **Do** keep charts and grids technical: explicit axes, semantic traces, tabular labels, local scrolling, and no horizontal page overflow.
- **Do** preserve keyboard focus, semantic status, reduced-motion behavior, responsive rails/drawers, and the persistent theme preference on new interactions.

### Don't:

- **Don't** call the product, design system, a theme, or a visual component Vela. ModelFiche is the user-facing and design-system identity.
- **Don't** collapse light and dark into a single palette or use a light-mode logo, reader symbol, or state color in dark mode.
- **Don't** use marketing-page ornament, decorative gradients, playful consumer styling, pop-up-heavy flows, queue-based planning metaphors, or static screenshot mockups that do not support comparison.
- **Don't** reach for a modal first. Prefer the established drawers and inspectors; the dedicated full-screen image lightbox is the explicit exception for visual review.
- **Don't** add rounded floating SaaS cards, soft ambient shadows, glassmorphism, color washes on inactive surfaces, or generic icon-card grids.
- **Don't** place decorative texture, imagery, or atmospheric effects inside technical charts and grid boards.
- **Don't** let a dense tool surface overflow horizontally. Wrap content, reduce responsive columns, use a drawer, or provide a local scroll/zoom surface.
