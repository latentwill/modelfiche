import { useEffect, useMemo, useState } from "react";
import { ArrowDownToLine, ArrowUpFromLine, ChevronLeft, ChevronRight, Maximize2, Minimize2, Star, X } from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Field, Form, Notice, Page, Panel } from "./ui";
import { galleryOpenHref } from "./screens-assets";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

export function DashboardScreen({ createProject = false }: { createProject?: boolean }) {
  const projects = useResource<unknown>("/api/projects?limit=100");
  const runs = useResource<unknown>("/api/runs?limit=100");
  const visible = rows(projects.data).filter(project => project.state !== "archived");
  const displayed = visible.length ? visible : rows(projects.data);
  const runRows = rows(runs.data);
  return <main className="vela-main">
    <section className="vela-hero"><div><h1>Dashboard</h1><p>Workspace overview: projects, model files, runs, and evaluation activity.</p></div><div className="vela-hero-art" aria-hidden="true"><img className="vela-reader-symbol vela-reader-symbol-light" src="/assets/modelfiche-reader-symbol-light.png" alt="" /><img className="vela-reader-symbol vela-reader-symbol-dark" src="/assets/modelfiche-reader-symbol-dark.png" alt="" /></div></section>
    {createProject && <section className="vela-section"><ProjectCreatePanel onCreated={projects.reload} /></section>}
    <section className="vela-section">
      <div className="vela-section-heading"><h2>Available projects</h2><a className="vela-text-button" href="#/projects">View all</a></div>
      <Notice error={projects.error} loading={projects.loading} empty={!displayed.length} />
      <div className="vela-project-grid">{displayed.map(project => <ProjectSummaryCard key={idOf(project)} project={project} />)}</div>
    </section>
    <section className="vela-section">
      <div className="vela-panel">
        <div className="vela-section-heading"><h2>Recent runs</h2><a className="vela-text-button" href="#/runs">View all</a></div>
        <Notice error={runs.error} loading={runs.loading} empty={!runRows.length} />
        <div className="vela-run-list">{runRows.map(run => <a className="vela-run-row" href={`#/run/${idOf(run)}`} key={idOf(run)}><span className="vela-run-mark" aria-hidden="true" /><span className="vela-run-copy"><h3>{str(run.name, "Training run")}<span className="vela-run-tag">{str(run.status, "unknown")}</span></h3><p>{str(run.base_model, "Base model not recorded")}</p></span><span className="vela-run-time">{formatDate(run.updated_at ?? run.created_at)}</span></a>)}</div>
      </div>
    </section>
  </main>;
}

function ProjectCreatePanel({ onCreated }: { onCreated: () => Promise<void> }) {
  return <Panel title="Create project"><Form submit="Create project" onSubmit={async form => { await api("/api/projects", jsonBody({ title: form.get("name"), description: form.get("description"), trigger_words: splitList(form.get("trigger_words")) })); await onCreated(); location.hash = "dashboard"; }}><div className="form-grid"><Field label="Project name"><input name="name" required autoFocus /></Field><Field label="Trigger words"><input name="trigger_words" /></Field></div><Field label="Description"><textarea name="description" /></Field></Form></Panel>;
}

function ProjectSummaryCard({ project }: { project: Row }) {
  const projectId = idOf(project);
  const models = useResource<unknown>(`/api/models${query({ project_id: projectId })}`);
  const runs = useResource<unknown>(`/api/runs${query({ project_id: projectId })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: projectId })}`);
  const evals = useResource<unknown>(`/api/eval-runs${query({ project_id: projectId })}`);
  const latestFal = useResource<unknown>(`/api/gallery${query({ project_id: projectId, kind: "image", category: "eval_output", provider: "fal", limit: 4 })}`);
  const previewImages = rows(latestFal.data).slice(0, 4);
  return <a className="vela-project-card" href={`#/project/${projectId}`}><div className="vela-project-card-body vela-project-card-heading"><h3>{str(project.title ?? project.name)}</h3><p className="vela-project-summary">{str(project.description, "Training workspace")}</p></div>{previewImages.length ? <div className="vela-project-preview" aria-label={`${previewImages.length} latest FAL images`}>{previewImages.map(image => <AssetImage key={idOf(image)} assetRevisionId={idOf(image)} alt="" loading="lazy" />)}</div> : <div className="vela-project-identity"><span>{initials(str(project.title ?? project.name))}</span><small>{str(project.state, "active")}</small></div>}<div className="vela-project-card-body"><div className="vela-project-data"><span className="vela-data-cell">Models <strong>{rows(models.data).length}</strong></span><span className="vela-data-cell">Runs <strong>{rows(runs.data).length}</strong></span><span className="vela-data-cell">Evals <strong>{rows(evals.data).length}</strong></span><span className="vela-data-cell">Datasets <strong>{rows(datasets.data).length}</strong></span></div></div></a>;
}


export function TransfersScreen({ mode = "", params = new URLSearchParams() }: { mode?: string; params?: URLSearchParams }) {
  const jobs = useResource<unknown>("/api/jobs?limit=100");
  const transfers = useResource<unknown>("/api/transfers");
  const [filter, setFilter] = useState("all");
  const [panel, setPanel] = useState(mode);
  const importRows = rows(jobs.data).filter(job => String(job.kind).includes("import")).map(job => ({ ...job, direction: "import" }));
  const exportRows = rows(transfers.data).map(job => ({ ...job, kind: "export.package", direction: "export" }));
  const data = [...exportRows, ...importRows].filter(job => filter === "all" || job.direction === filter);
  return <Page title="Import / Export History" subtitle="Transfer training runs and DAM packages without overwriting existing work." actions={<><button aria-pressed={panel === "import"} onClick={() => setPanel(panel === "import" ? "" : "import")}><ArrowDownToLine size={16} /> Import</button><button className="primary" aria-pressed={panel === "export"} onClick={() => setPanel(panel === "export" ? "" : "export")}><ArrowUpFromLine size={16} /> Export</button></>}>
    {panel === "import" && <TransferImport />}
    {panel === "export" && <TransferExport onCreated={transfers.reload} params={params} />}
    <Panel><div className="tabs" role="tablist" aria-label="Transfer filters">{["all", "export", "import"].map(value => <button role="tab" aria-selected={filter === value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)} key={value}>{value[0].toUpperCase() + value.slice(1)}</button>)}</div>
      <Notice error={jobs.error || transfers.error} loading={jobs.loading || transfers.loading} empty={!data.length} /><DataTable rows={data} columns={[["name", "Name"], ["direction", "Type"], ["state", "State"], ["progress", "Progress"], ["created_at", "Date"]]} onRow={job => { if (job.direction === "export" && job.download_ready) window.location.href = `/api/transfers/${idOf(job)}/download`; else location.hash = `jobs/${idOf(job)}?job=1`; }} />
    </Panel>
  </Page>;
}

function TransferImport() {
  const sources = useResource<unknown>("/api/import-sources");
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [message, setMessage] = useState("");
  return <Panel title="Import AI Toolkit run or dataset"><Form submit="Start import" onSubmit={async form => {
    const sourceId = String(form.get("source_id")); const prefix = String(form.get("prefix"));
    const detection = await api<Row>(`/api/import-sources/${sourceId}/detect`, jsonBody({ prefix }));
    const created = await api<Row>("/api/import-jobs", jsonBody({ source_id: sourceId, project_id: form.get("project_id"), prefix, kind: detection.kind }));
    setMessage(`${str(detection.kind, "Unknown")} detected at ${Math.round(Number(detection.confidence ?? 0) * 100)}% confidence. Import queued as ${str(created.id)}.`);
  }}><div className="form-grid"><Field label="S3 source"><select name="source_id" required><option value="">Select...</option>{rows(sources.data).map(source => <option value={idOf(source)} key={idOf(source)}>{str(source.name)}</option>)}</select></Field><Field label="Target project"><select name="project_id" required><option value="">Select...</option>{rows(projects.data).map(project => <option value={idOf(project)} key={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field><Field label="Prefix"><input name="prefix" required /></Field></div></Form>{message && <div className="vela-notice" role="status"><strong>Import queued</strong><span>{message}</span></div>}<a href="#/import">Open full S3 browser</a></Panel>;
}

function TransferExport({ onCreated, params }: { onCreated: () => Promise<void>; params: URLSearchParams }) {
  const projects = useResource<unknown>("/api/projects?limit=100"); const [message, setMessage] = useState("");
  const target = transferTarget(params);
  const targetResource = useResource<Row>(target?.path ?? null);
  const targetName = str(targetResource.data?.name ?? targetResource.data?.title, target ? `${target.label} ${target.id}` : "");
  const targetUnavailable = Boolean(target && (targetResource.error || !targetResource.data));
  return <Panel title="Export"><Notice error={targetResource.error || projects.error} loading={targetResource.loading || projects.loading} />{!targetUnavailable && <Form submit="Create export" onSubmit={async form => {
    const created = await api<Row>("/api/transfers", jsonBody({
      name: form.get("name"),
      project_id: target?.kind === "project" ? target.id : target ? null : form.get("project_id"),
      asset_ids: target?.kind === "asset" ? [target.id] : [],
      dataset_version_ids: target?.kind === "dataset_version" ? [target.id] : [],
      model_version_ids: target?.kind === "model_version" ? [target.id] : [],
      eval_run_ids: target?.kind === "eval" ? [target.id] : [],
      include_files: form.get("include_files") === "on",
      include_reviews: form.get("include_reviews") === "on",
    }));
    setMessage(`Export ${str(created.name)} queued.`); await onCreated();
  }}><div className="form-grid"><Field label="Package name"><input key={targetName || "manual"} name="name" defaultValue={targetName} required /></Field>{target ? <Field label="Export scope"><input readOnly value={`${target.label}: ${targetName}`} /></Field> : <Field label="Export project"><select name="project_id" required><option value="">Select project...</option>{rows(projects.data).map(project => <option value={idOf(project)} key={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>}</div><label><input name="include_files" type="checkbox" defaultChecked /> Include available hydrated files</label><label><input name="include_reviews" type="checkbox" defaultChecked /> Include comments, ratings, and review metadata</label></Form>}{message && <div className="vela-notice" role="status"><strong>Export queued</strong><span>{message}</span></div>}</Panel>;
}

export function ImageReviewScreen({ id }: { id: string }) {
  const asset = useResource<Row>(`/api/assets/${id}`);
  const context = useResource<Row>(`/api/assets/${id}/context`);
  const gallery = useResource<unknown>(asset.data ? `/api/gallery${query({ project_id: asset.data.project_id, kind: asset.data.kind, limit: 250 })}` : null);
  const comments = useResource<unknown>(`/api/comments${query({ subject_type: "asset", subject_id: id })}`);
  const reviews = useResource<unknown>(`/api/reviews${query({ subject_type: "asset", subject_id: id })}`);
  const items = rows(gallery.data); const index = items.findIndex(item => idOf(item) === id);
  const previous = index > 0 ? items[index - 1] : undefined; const next = index >= 0 ? items[index + 1] : undefined;
  const [fit, setFit] = useState(true);
  const [ratingBusy, setRatingBusy] = useState(false);
  const [ratingError, setRatingError] = useState("");
  const currentRating = Number(rows(reviews.data)[0]?.rating ?? 0);
  const metadata = context.data?.metadata && typeof context.data.metadata === "object" ? context.data.metadata as Row : {};
  return <div className="review-overlay vela-focus-workspace"><header className="viewer-topbar"><a href="#/gallery">Gallery</a><div className="viewer-actions"><button title={fit ? "Fill image" : "Fit image"} aria-label={fit ? "Fill image" : "Fit image"} onClick={() => setFit(!fit)}>{fit ? <Maximize2 size={16} /> : <Minimize2 size={16} />}<span>{fit ? "Fill" : "Fit"}</span></button><a className="button" aria-label="Close image review" href="#/gallery"><X size={16} /> Close</a></div></header>
    <main className="image-review-stage"><div className="image-review-main"><div className="image-nav"><a aria-disabled={!previous} href={previous ? `#/image/${idOf(previous)}` : undefined}><ChevronLeft size={16} /> Previous</a><strong>{str(asset.data?.name, "Image")}</strong><a aria-disabled={!next} href={next ? `#/image/${idOf(next)}` : undefined}>Next <ChevronRight size={16} /></a></div><AssetImage className={fit ? "fit" : "fill"} assetRevisionId={id} variant="content" alt={str(asset.data?.name, "Selected image")} />
      <div className="filmstrip">{items.slice(Math.max(0, index - 4), index + 5).map(item => <a className={idOf(item) === id ? "active" : ""} href={`#/image/${idOf(item)}`} key={idOf(item)}><AssetImage assetRevisionId={idOf(item)} alt={str(item.name)} /></a>)}</div></div>
      <aside id="storage_details" className="image-review-inspector"><h2>Image review</h2><Notice error={asset.error || context.error || gallery.error} loading={asset.loading || context.loading} /><dl className="details"><dt>File</dt><dd>{str(asset.data?.name)}</dd><dt>Type</dt><dd>{str(asset.data?.mime_type)}</dd><dt>Kind</dt><dd>{str(asset.data?.kind)}</dd><dt>Dimensions</dt><dd>{str(metadata.width ?? (context.data?.image as Row | undefined)?.width)} × {str(metadata.height ?? (context.data?.image as Row | undefined)?.height)}</dd><dt>Model</dt><dd>{str(metadata.model_name, "Unknown")}</dd><dt>Base model</dt><dd>{str(metadata.base_model, "Unknown")}</dd><dt>Dataset</dt><dd>{str(metadata.dataset_name, "Unknown")}</dd><dt>Training run</dt><dd>{str(metadata.run_name, "Unknown")}</dd><dt>Step</dt><dd>{str(metadata.step, "Unknown")}</dd><dt>Seed</dt><dd>{str(metadata.seed, "Unknown")}</dd><dt>Storage</dt><dd>{rows((context.data?.asset as Row | undefined)?.locations).map(location => str(location.provider)).join(", ") || "Remote"}</dd></dl><details><summary>All metadata</summary><pre className="vela-code">{JSON.stringify(metadata, null, 2)}</pre></details>
        <div className="rating-control"><span>Image rating</span>{[1,2,3,4,5].map(value => <button className="vela-rating-button" aria-label={`${value} stars`} aria-pressed={currentRating === value} disabled={ratingBusy} key={value} onClick={async () => { setRatingBusy(true); setRatingError(""); try { await api("/api/reviews", jsonBody({ subject_type: "asset", subject_id: id, rating: value, decision: "candidate" })); await reviews.reload(); } catch (reason) { setRatingError(reason instanceof Error ? reason.message : String(reason)); } finally { setRatingBusy(false); } }}><Star size={16} fill={currentRating >= value ? "currentColor" : "none"} /></button>)}</div>{ratingError && <div className="vela-notice vela-notice-error" role="alert">{ratingError}</div>}
        <section><h3>Comments</h3><div className="comments">{rows(comments.data).map(comment => <article key={idOf(comment)}><strong>{str(comment.profile_name, "Operator")}</strong><p>{str(comment.body)}</p></article>)}</div><Form submit="Add comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: "asset", subject_id: id, body: form.get("body") })); await comments.reload(); }}><Field label="Comment"><textarea name="body" required /></Field></Form></section>
      </aside></main>
  </div>;
}

export function SampleViewerScreen({ runId, initialStep = "" }: { runId: string; initialStep?: string }) {
  const run = useResource<Row>(`/api/runs/${runId}`);
  const samples = useResource<unknown>(`/api/runs/${runId}/samples`);
  const sampleRows = rows(samples.data);
  const groups = useMemo(() => groupBy(sampleRows, item => str(item.step, "Unassigned")), [samples.data]);
  const [selectedSteps, setSelectedSteps] = useState<string[]>(() => initialStep ? [initialStep] : []);
  const [ratings, setRatings] = useState<Record<string, number>>({});
  const [ratingBusy, setRatingBusy] = useState("");
  const [ratingError, setRatingError] = useState("");
  useEffect(() => { if (initialStep && groups[initialStep]) setSelectedSteps([initialStep]); }, [groups, initialStep]);
  useEffect(() => { const persisted: Record<string, number> = {}; sampleRows.forEach(item => { const assetId = str(item.asset_revision_id ?? item.asset_id ?? item.id); if (item.rating) persisted[assetId] = Number(item.rating); }); setRatings(persisted); }, [samples.data]);
  const shown = Object.entries(groups).filter(([step]) => !selectedSteps.length || selectedSteps.includes(step)).slice(0, 6);
  return <div className="comparison-viewer vela-focus-workspace"><header className="viewer-topbar"><a href={`#/run/${runId}`}>{str(run.data?.name, "Training run")}</a><div className="viewer-actions">{Boolean(run.data?.model_id) && <a className="button" href={`#/model/${str(run.data?.model_id)}`}>Back to model</a>}<a className="button" aria-label="Close sample viewer" href={`#/run/${runId}`}><X size={16} /> Close</a></div></header>
    <div className="comparison-toolbar"><strong>Sample viewer</strong><div className="chiplets">{Object.keys(groups).map(step => <button aria-pressed={selectedSteps.includes(step)} className={selectedSteps.includes(step) ? "active" : ""} key={step} onClick={() => setSelectedSteps(toggleLimited(selectedSteps, step, 6))}>Step {step}</button>)}</div><span>{selectedSteps.length}/6 selected</span>{ratingError && <small role="alert">{ratingError}</small>}</div>
    <main className="comparison-columns"><Notice error={samples.error} loading={samples.loading} empty={!shown.length} />{shown.map(([step, items]) => <section className="comparison-column" key={step}><header><strong>Step {step}</strong><span>{items.length} samples</span></header><Form submit="Add group comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: "training_run", subject_id: runId, body: `[Step ${step}] ${String(form.get("body"))}` })); }}><Field label={`Step ${step} comment`}><textarea name="body" required /></Field></Form>{items.map(item => { const assetId = str(item.asset_revision_id ?? item.asset_id ?? item.id); return <article className="comparison-image" key={idOf(item)}><a href={galleryOpenHref({ ...item, project_id: run.data?.project_id, origin_type: "SAMPLE" })}><AssetImage assetRevisionId={assetId} alt={str(item.name, `Step ${step} sample`)} loading="lazy" /></a><div className="rating-control compact">{[1,2,3,4,5].map(value => <button className="vela-rating-button" aria-label={`${value} stars`} aria-pressed={ratings[assetId] === value} disabled={ratingBusy === assetId} key={value} onClick={async () => { setRatingBusy(assetId); setRatingError(""); try { await api("/api/reviews", jsonBody({ subject_type: "asset", subject_id: assetId, rating: value, decision: "candidate" })); setRatings(current => ({ ...current, [assetId]: value })); } catch (reason) { setRatingError(reason instanceof Error ? reason.message : String(reason)); } finally { setRatingBusy(""); } }}><Star size={14} fill={(ratings[assetId] ?? 0) >= value ? "currentColor" : "none"} /></button>)}</div></article>; })}</section>)}</main>
  </div>;
}

function splitList(value: FormDataEntryValue | null) { return String(value ?? "").split(",").map(item => item.trim()).filter(Boolean); }
function initials(value: string) { return value.split(/\s+/).map(part => part[0]).join("").slice(0, 2).toUpperCase(); }
function formatDate(value: unknown) { const date = new Date(String(value ?? "")); return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
function groupBy(items: Row[], key: (item: Row) => string) { return items.reduce<Record<string, Row[]>>((groups, item) => { const value = key(item); (groups[value] ??= []).push(item); return groups; }, {}); }
function toggleLimited(values: string[], value: string, max: number) { return values.includes(value) ? values.filter(item => item !== value) : values.length < max ? [...values, value] : values; }
function transferTarget(params: URLSearchParams) {
  const definitions = [
    ["project", "Project", "projects"],
    ["asset", "Asset", "assets"],
    ["dataset_version", "Dataset version", "dataset-versions"],
    ["model_version", "Model version", "model-versions"],
    ["eval", "Eval run", "eval-runs"],
  ] as const;
  for (const [kind, label, route] of definitions) { const id = params.get(kind); if (id) return { kind, label, id, path: `/api/${route}/${id}` }; }
  return null;
}
