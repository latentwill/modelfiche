# Product

## Register

product

## Users

Creative operators, model trainers, reviewers, and production leads who need a visual workbench for model-training assets. They need to move between source images, user-defined projects, trigger-word metadata, training runs, dataset versions, `.safetensors` model files, generated samples, captions, ratings, and lineage without losing context.

## Product Purpose

This product is a DAM-style inspection and review layer around visual model training. A Workspace is the larger client or job container. Each Project is a free-form user-defined scope, such as an artist name, subject, concept, product pack, campaign, or trigger-word label; a Project can carry one or many trigger words as metadata. Runs contain the concrete outputs: dataset versions, `.safetensors` model files, eval batches, generated samples, and lineage. AI Toolkit handles training runs, agents handle heavy batch operations, and this app gives humans a tool surface for asset curation, dataset inspection, model-file registration, Eval testing, generated sample comparison, feedback capture, and artifact lineage.

The dashboard is a workspace-level view: it should summarize available projects as visual square cards, expose lightweight metadata like model count, run count, dataset count, trigger-word count, and show a recent activity feed of imports, evals, model-file registrations, exports, and review events. The project view is the deeper workbench for comparing runs, project-contained dataset subsets, `.safetensors` model files, eval batches, sample outputs, and lineage inside one selected project. The dataset view is the caption and inclusion workbench for a selected project's dataset subset: dataset metadata at the top, a caption version tracker, every image with full editable captions, text-vs-JSON caption type tags, scoped find/replace, add-word batch edits, and multiselect. Under project Evals, the Eval generator creates FAL eval runs from endpoint-specific schemas, automatic model upload/status metadata, prompt-set JSON files, and LLM-assisted prompt-set generation. The Grid generator is a separate project Eval surface for two-axis sweeps such as Prompt x Steps, Steps x Strength, Steps x Seed, and Prompt x Model; saved Eval Grids are composed in the app from discrete generated images with zoom, scan, and expand controls for large sweeps. Grid previews and saved grids open a dedicated Grid lightbox for full-screen zoom and scan inspection without ratings, comments, or a metadata sidebar. Under Models, the Eval viewer compares prompt sets only, while the Sample viewer compares training-run steps only; prompt sets and steps are separate surfaces with side-by-side image columns and ratings, without per-image comment fields. Eval and Sample image cards expose a Full screen action into the shared image lightbox when the user needs full-size inspection. Project and dataset inspectors support markdown notes with selectable note history for review decisions and follow-up context. Top-level creation, import, and upload actions live under the global `+` menu; page headers should avoid duplicating New Project, Import, Upload, and Eval controls.

## Brand Personality

Serious, precise, production-minded. The product should feel like a creative operations tool with enough density for expert work and enough whitespace to keep comparison and review decisions clear.

## Anti-references

Avoid marketing-page ornament, decorative gradients, playful consumer styling, pop-up heavy flows, queue-based planning metaphors, and static screenshot mockups that do not support comparison. Avoid modal-first interactions; drawers and inspectors are preferred for maintaining workspace context. The exception is a dedicated full-screen image lightbox for Dataset and Gallery inspection, because image review needs maximal visual area plus metadata and caption context.

## Design Principles

- Keep comparison central: runs, datasets, `.safetensors` model files, and sample outputs should be easy to scan side by side inside the selected project scope.
- Split overview from inspection: the dashboard should support workspace scanning across projects, while project pages should support detailed run/model/dataset comparison.
- Preserve context: selecting a workspace, project, run, model file, dataset, or sample should open a right-side context panel with metadata first and that object's activity feed below it.
- Keep Gallery broad: Gallery is the single image surface for source assets, generated samples, eval outputs, and other visual artifacts.
- Keep Evals project-contained: Eval generation, prompt sets, saved grids, and generated eval outputs live under the selected project Evals tree, while Operations stays limited to export workflows.
- Make endpoint schemas mutable: FAL eval generation should adapt its controls to the selected provider endpoint, such as Ideogram 4 versus Krea 2 LoRA.
- Split Eval and Grid generation: Eval generation handles model status, endpoint schema, prompt-set JSON, and generating eval runs; Grid generation handles endpoint-derived two-axis matrix sweeps and saved grid definitions.
- Save grid definitions: Eval Grids are first-class saved Eval objects composed from many individual generated images, not giant rendered grid images; large grids need zoom, scan, and expand affordances.
- Inspect grids full screen: saved Eval Grids should open a dedicated Grid lightbox focused on zoom and scan controls only, with no rating, comment, or sidebar metadata surfaces.
- Keep Datasets project-contained: datasets should appear as subsets inside a selected project, not as their own top-level menu destination.
- Keep dataset editing batch-aware: image caption edits should work across selected images first, then the visible dataset when nothing is selected.
- Track caption versions and formats: dataset caption work should show caption version state, and distinguish normal text captions from JSON captions in both per-image rows and caption operations.
- Keep review notes scoped and editable: project and dataset inspectors should support markdown notes, formatting controls, Add Note, and selectable previous notes.
- Use a full-screen image lightbox for visual inspection: Dataset, Gallery, Eval, and Sample images should expose a Full screen action that opens a dedicated lightbox with image metadata, 5-star rating, image comments, caption, and image-scoped activity.
- Separate tool surfaces clearly: gallery, datasets, runs, model files, eval batches, exports, and agent operations should read as distinct workbench layers.
- Make technical states visible: eval status, dataset inclusion, model file, trigger-word coverage, rejected samples, and output quality should be legible at dashboard density.
- Favor production ergonomics: clean structure, stable navigation, fast scanning, and no decorative UI that competes with assets.
- Use vertical scrolling only: workbench pages, viewers, and generated grid surfaces must prevent horizontal page scrolling; dense content should wrap, collapse, zoom, or use vertical-only panes.

## Accessibility & Inclusion

Use readable contrast, keyboard-friendly controls, clear focus targets, reduced-motion-safe behavior, and responsive layouts that preserve tool hierarchy across desktop, tablet, and mobile web widths.
