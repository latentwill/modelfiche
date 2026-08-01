import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownToLine, ArrowUpFromLine, ChevronLeft, ChevronRight, Star, X } from "lucide-react";
import { api, idOf, jsonBody, listOf, query, routeQuery, str } from "./api";
import { ActiveTrainingPanel } from "./active-training";
import { previewAssetId } from "./activity";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Dropdown, Field, Form, Notice, Page, Panel, Status } from "./ui";
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
    <section className="vela-hero"><div><h1>Dashboard</h1><p>Workspace overview: projects, model files, runs, and grid activity.</p></div><div className="vela-hero-art" aria-hidden="true"><img className="vela-reader-symbol vela-reader-symbol-light" src="/assets/modelfiche-reader-symbol-light.png" alt="" /><img className="vela-reader-symbol vela-reader-symbol-dark" src="/assets/modelfiche-reader-symbol-dark.png" alt="" /></div></section>
    {createProject && <section className="vela-section"><ProjectCreatePanel onCreated={projects.reload} /></section>}
    <ActiveTrainingPanel />
    <section className="vela-section">
      <div className="vela-section-heading"><h2>Available projects</h2><a className="vela-text-button" href={`#/projects${routeQuery({})}`} aria-label="View all projects">View all</a></div>
      <Notice error={projects.error} loading={projects.loading} empty={!displayed.length} />
      <div className="vela-project-grid">{displayed.map(project => <ProjectSummaryCard key={idOf(project)} project={project} />)}</div>
    </section>
    <section className="vela-section">
      <div className="vela-panel">
        <div className="vela-section-heading"><h2>Recent runs</h2><a className="vela-text-button" href={`#/runs${routeQuery({})}`} aria-label="View all recent runs">View all</a></div>
        <Notice error={runs.error} loading={runs.loading} empty={!runRows.length} />
        <div className="vela-run-list">{runRows.map(run => { const previewId = previewAssetId(run.preview_asset_id); return <a className="vela-run-row" href={`#/run/${idOf(run)}${routeQuery({ project: run.project_id })}`} key={idOf(run)}>{previewId ? <AssetImage assetRevisionId={previewId} alt="" style={{ width: "28px", height: "28px", objectFit: "cover", borderRadius: "50%" }} /> : <span className="vela-run-mark" aria-hidden="true" />}<span className="vela-run-copy"><h3>{str(run.name, "Training run")}<span className="vela-run-tag">{str(run.status, "unknown")}</span></h3><p>{str(run.base_model, "Base model not recorded")}</p></span><span className="vela-run-time">{formatDate(run.updated_at ?? run.created_at)}</span></a>; })}</div>
      </div>
    </section>
  </main>;
}

function ProjectCreatePanel({ onCreated }: { onCreated: () => Promise<void> }) {
  return <Panel title="Create project"><Form submit="Create project" onSubmit={async form => { await api("/api/projects", jsonBody({ title: form.get("name"), description: form.get("description"), trigger_words: splitList(form.get("trigger_words")) })); await onCreated(); location.hash = `dashboard${routeQuery({})}`; }}><div className="form-grid"><Field label="Project name"><input name="name" required autoFocus /></Field><Field label="Trigger words"><input name="trigger_words" /></Field></div><Field label="Description"><textarea name="description" /></Field></Form></Panel>;
}

function ProjectSummaryCard({ project }: { project: Row }) {
  const projectId = idOf(project);
  const models = useResource<unknown>(`/api/models${query({ project_id: projectId })}`);
  const runs = useResource<unknown>(`/api/runs${query({ project_id: projectId })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: projectId })}`);
  const grids = useResource<unknown>(`/api/projects/${projectId}/grids`);
  const gridImages = useResource<unknown>(`/api/gallery${query({ project_id: projectId, kind: "image", category: "eval_output", limit: 4 })}`);
  const sampleImages = useResource<unknown>(`/api/gallery${query({ project_id: projectId, kind: "image", category: "sample", limit: 4 })}`);
  const datasetImages = useResource<unknown>(`/api/gallery${query({ project_id: projectId, kind: "image", category: "dataset_image", include_dataset_assets: true, limit: 4 })}`);
  const previewSource = rows(gridImages.data).length
    ? { images: rows(gridImages.data), label: "grid outputs" }
    : rows(sampleImages.data).length
      ? { images: rows(sampleImages.data), label: "training samples" }
      : { images: rows(datasetImages.data), label: "dataset images" };
  const previewImages = previewSource.images.slice(0, 4);
  return <a className="vela-project-card" href={`#/project/${projectId}${routeQuery({})}`}><div className="vela-project-card-body vela-project-card-heading"><h3>{str(project.title ?? project.name)}</h3><p className="vela-project-summary">{str(project.description, "Training workspace")}</p></div>{previewImages.length ? <div className="vela-project-preview" aria-label={`${previewImages.length} latest ${previewSource.label}`}>{previewImages.map(image => <AssetImage key={idOf(image)} assetRevisionId={idOf(image)} alt="" loading="lazy" />)}</div> : <div className="vela-project-identity"><span>{initials(str(project.title ?? project.name))}</span><small>{str(project.state, "active")}</small></div>}<div className="vela-project-card-body"><div className="vela-project-data"><span className="vela-data-cell">Models <strong>{rows(models.data).length}</strong></span><span className="vela-data-cell">Runs <strong>{rows(runs.data).length}</strong></span><span className="vela-data-cell">Grids <strong>{rows(grids.data).length}</strong></span><span className="vela-data-cell">Datasets <strong>{rows(datasets.data).length}</strong></span></div></div></a>;
}


export function TransfersScreen({ mode = "", params = new URLSearchParams() }: { mode?: string; params?: URLSearchParams }) {
  const jobs = useResource<unknown>("/api/jobs?limit=100");
  const transfers = useResource<unknown>("/api/transfers");
  const [filter, setFilter] = useState("all");
  const [panel, setPanel] = useState(mode);
  const importRows = rows(jobs.data).filter(job => String(job.kind).includes("import")).map(job => {
    const payload = job.payload && typeof job.payload === "object" ? job.payload as Row : {};
    const prefix = str(payload.prefix, "S3 source");
    return { ...job, name: `Import · ${prefix}`, direction: "import", direction_label: "Import", progress_label: `${Math.round(Number(job.progress ?? 0) * 100)}%` };
  });
  const exportRows = rows(transfers.data).map(job => ({ ...job, name: str(job.name, "Export package"), kind: "export.package", direction: "export", direction_label: "Export", progress_label: `${Math.round(Number(job.progress ?? 0) * 100)}%` }));
  const data = [...exportRows, ...importRows].filter(job => filter === "all" || job.direction === filter);
  return <Page title="Import / Export History" subtitle="Transfer training runs and DAM packages without overwriting existing work." actions={<><button aria-pressed={panel === "import"} onClick={() => setPanel(panel === "import" ? "" : "import")}><ArrowDownToLine size={16} /> Import</button><button className="primary" aria-pressed={panel === "export"} onClick={() => setPanel(panel === "export" ? "" : "export")}><ArrowUpFromLine size={16} /> Export</button></>}>
    {panel === "import" && <TransferImport params={params} />}
    {panel === "export" && <TransferExport onCreated={transfers.reload} params={params} />}
    <Panel><div className="tabs" role="tablist" aria-label="Transfer filters">{["all", "export", "import"].map(value => <button role="tab" aria-selected={filter === value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)} key={value}>{value[0].toUpperCase() + value.slice(1)}</button>)}</div>
      <Notice error={jobs.error || transfers.error} loading={jobs.loading || transfers.loading} empty={!data.length} /><DataTable rows={data} columns={[["name", "Transfer"], ["direction_label", "Direction"], ["state", "Status"], ["progress_label", "Progress"], ["created_at", "Started"]]} onRow={job => { if (job.direction === "export" && job.download_ready) window.location.href = `/api/transfers/${idOf(job)}/download`; else location.hash = `jobs/${idOf(job)}${routeQuery({ job: 1, project: job.project_id })}`; }} />
    </Panel>
  </Page>;
}

function TransferImport({ params }: { params: URLSearchParams }) {
  const sources = useResource<unknown>("/api/import-sources");
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [message, setMessage] = useState("");
  return <Panel title="Import AI Toolkit run or dataset"><Form submit="Start import" onSubmit={async form => {
    const sourceId = String(form.get("source_id")); const prefix = String(form.get("prefix"));
    const detection = await api<Row>(`/api/import-sources/${sourceId}/detect`, jsonBody({ prefix }));
    const created = await api<Row>("/api/import-jobs", jsonBody({ source_id: sourceId, project_id: form.get("project_id"), prefix, kind: detection.kind }));
    setMessage(`${str(detection.kind, "Unknown")} detected at ${Math.round(Number(detection.confidence ?? 0) * 100)}% confidence. Import queued as ${str(created.id)}.`);
  }}><div className="form-grid"><Field label="S3 source"><Dropdown name="source_id" options={rows(sources.data).map(source => ({ value: idOf(source), label: str(source.name) }))} required placeholder="Select..." /></Field><Field label="Target project"><Dropdown name="project_id" options={rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))} required placeholder="Select..." /></Field><Field label="Prefix"><input name="prefix" required /></Field></div></Form>{message && <div className="vela-notice" role="status"><strong>Import queued</strong><span>{message}</span></div>}<a href={`#/import${query({ source: "s3", project: params.get("project") })}`}>Open full S3 browser</a></Panel>;
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
  }}><div className="form-grid"><Field label="Package name"><input key={targetName || "manual"} name="name" defaultValue={targetName} required /></Field>{target ? <Field label="Export scope"><input readOnly value={`${target.label}: ${targetName}`} /></Field> : <Field label="Export project"><Dropdown name="project_id" options={rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))} required placeholder="Select project..." /></Field>}</div><label><input name="include_files" type="checkbox" defaultChecked /> Include available hydrated files</label><label><input name="include_reviews" type="checkbox" defaultChecked /> Include comments, ratings, and review metadata</label></Form>}{message && <div className="vela-notice" role="status"><strong>Export queued</strong><span>{message}</span></div>}</Panel>;
}


const MAX_SAMPLE_COLUMNS = 6;
const SAMPLE_ROWS_PER_PAGE = 8;

export type SampleComparisonColumn = {
  id: string;
  step: number | null;
  label: string;
  checkpointId: string;
  checkpointFilename: string;
  checkpointState: string;
  modelVersionId: string;
  modelVersionName: string;
  localStatus: string;
};

export type SampleComparisonRow = {
  id: string;
  label: string;
  ordinal: number | null;
  cells: Record<string, Row>;
};

function sampleColumnId(sample: Row) {
  const checkpointId = str(sample.checkpoint_id, "");
  if (checkpointId) return checkpointId;
  return `step:${str(sample.training_step, "unassigned")}:${str(sample.sample_track, "default")}`;
}

export function sampleComparisonColumns(samples: Row[]): SampleComparisonColumn[] {
  const unique = new Map<string, SampleComparisonColumn>();
  samples.forEach(sample => {
    const id = sampleColumnId(sample);
    if (unique.has(id)) return;
    const rawStep = Number(sample.training_step);
    const step = Number.isFinite(rawStep) ? rawStep : null;
    const track = str(sample.sample_track, "");
    const readiness = sample.checkpoint_readiness && typeof sample.checkpoint_readiness === "object" ? sample.checkpoint_readiness as Row : {};
    const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
    unique.set(id, {
      id,
      step,
      label: `${track ? `${track[0].toUpperCase()}${track.slice(1)} · ` : ""}${step == null ? "Step not recorded" : `Step ${step}`}`,
      checkpointId: str(sample.checkpoint_id, ""),
      checkpointFilename: str(sample.checkpoint_filename, ""),
      checkpointState: str(sample.checkpoint_state, "unknown"),
      modelVersionId: str(sample.model_version_id, ""),
      modelVersionName: str(sample.model_version_name, ""),
      localStatus: str(local.status, "unknown"),
    });
  });
  return [...unique.values()].sort((left, right) => (left.step ?? Number.MAX_SAFE_INTEGER) - (right.step ?? Number.MAX_SAFE_INTEGER) || left.label.localeCompare(right.label));
}

export function sampleComparisonRows(samples: Row[]): SampleComparisonRow[] {
  const unique = new Map<string, SampleComparisonRow>();
  samples.forEach(sample => {
    const id = str(sample.sample_identity, `sample-record:${idOf(sample)}`);
    const current = unique.get(id) ?? {
      id,
      label: str(sample.sample_label ?? sample.prompt, "Unlabelled sample"),
      ordinal: Number.isFinite(Number(sample.sample_ordinal)) ? Number(sample.sample_ordinal) : null,
      cells: {},
    };
    current.cells[sampleColumnId(sample)] = sample;
    unique.set(id, current);
  });
  return [...unique.values()].sort((left, right) => (left.ordinal ?? Number.MAX_SAFE_INTEGER) - (right.ordinal ?? Number.MAX_SAFE_INTEGER) || left.label.localeCompare(right.label));
}

function sampleViewerParams() {
  if (typeof location === "undefined") return new URLSearchParams();
  return new URLSearchParams(location.hash.split("?", 2)[1] ?? "");
}

function safeSampleReturn(value: string | null) {
  if (!value || value.startsWith("/") || value.startsWith("#") || /^[a-z]+:/i.test(value)) return "";
  return value;
}

function updateSampleViewerUrl(runId: string, selected: string[], page: number) {
  if (typeof history === "undefined" || typeof location === "undefined") return;
  const params = sampleViewerParams();
  params.delete("step");
  if (selected.length) params.set("checkpoints", selected.join(","));
  else params.delete("checkpoints");
  if (page > 1) params.set("page", String(page));
  else params.delete("page");
  const suffix = params.toString();
  history.replaceState(null, "", `#/samples/${runId}${suffix ? `?${suffix}` : ""}`);
}

export function SampleViewerScreen({ runId, initialStep = "" }: { runId: string; initialStep?: string }) {
  const run = useResource<Row>(`/api/runs/${runId}`);
  const samples = useResource<unknown>(`/api/runs/${runId}/samples?limit=1000`);
  const sampleRows = rows(samples.data);
  const columns = useMemo(() => sampleComparisonColumns(sampleRows), [samples.data]);
  const matrixRows = useMemo(() => sampleComparisonRows(sampleRows), [samples.data]);
  const initialParams = useMemo(() => sampleViewerParams(), [runId]);
  const requestedColumns = useMemo(() => (initialParams.get("checkpoints") ?? "").split(",").filter(Boolean), [initialParams]);
  const initialPage = Math.max(1, Number(initialParams.get("page")) || 1);
  const returnTarget = safeSampleReturn(initialParams.get("return")) || `run/${runId}`;
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [selectionMessage, setSelectionMessage] = useState("");
  const [page, setPage] = useState(initialPage);
  const [activeAssetId, setActiveAssetId] = useState("");
  const [mobilePromptId, setMobilePromptId] = useState("");
  const [mobileColumnId, setMobileColumnId] = useState("");
  const initialized = useRef(false);
  const selected = columns.filter(column => selectedColumns.includes(column.id));
  const pageCount = Math.max(1, Math.ceil(matrixRows.length / SAMPLE_ROWS_PER_PAGE));
  const safePage = Math.min(page, pageCount);
  const visibleRows = matrixRows.slice((safePage - 1) * SAMPLE_ROWS_PER_PAGE, safePage * SAMPLE_ROWS_PER_PAGE);
  const activeSample = sampleRows.find(sample => str(sample.asset_revision_id ?? sample.asset_id ?? sample.id) === activeAssetId) ?? null;
  const mobileRow = visibleRows.find(row => row.id === mobilePromptId) ?? visibleRows[0];
  const mobileColumn = selected.find(column => column.id === mobileColumnId) ?? selected[0];
  const mobileSample = mobileRow && mobileColumn ? mobileRow.cells[mobileColumn.id] : null;

  useEffect(() => {
    if (initialized.current || !columns.length) return;
    const requested = requestedColumns.filter(id => columns.some(column => column.id === id));
    const legacy = initialStep ? columns.filter(column => String(column.step) === initialStep).map(column => column.id) : [];
    const defaults = (requested.length ? requested : legacy.length ? legacy : columns.slice(0, MAX_SAMPLE_COLUMNS).map(column => column.id)).slice(0, MAX_SAMPLE_COLUMNS);
    initialized.current = true;
    setSelectedColumns(defaults);
    setMobileColumnId(defaults[0] ?? "");
    updateSampleViewerUrl(runId, defaults, initialPage);
  }, [columns, initialPage, initialStep, requestedColumns, runId]);

  useEffect(() => {
    if (safePage !== page) {
      setPage(safePage);
      updateSampleViewerUrl(runId, selectedColumns, safePage);
    }
  }, [page, runId, safePage, selectedColumns]);

  useEffect(() => {
    if (!visibleRows.some(row => row.id === mobilePromptId)) setMobilePromptId(visibleRows[0]?.id ?? "");
  }, [mobilePromptId, visibleRows]);

  const selectColumn = (columnId: string) => {
    const next = selectedColumns.includes(columnId)
      ? selectedColumns.filter(id => id !== columnId)
      : selectedColumns.length < MAX_SAMPLE_COLUMNS
        ? [...selectedColumns, columnId]
        : selectedColumns;
    if (next === selectedColumns) {
      setSelectionMessage(`Choose at most ${MAX_SAMPLE_COLUMNS} checkpoint columns. Remove one before adding another.`);
      return;
    }
    setSelectionMessage(`${next.length} of ${MAX_SAMPLE_COLUMNS} checkpoint columns selected.`);
    setSelectedColumns(next);
    setMobileColumnId(current => next.includes(current) ? current : next[0] ?? "");
    setPage(1);
    updateSampleViewerUrl(runId, next, 1);
  };

  const changePage = (next: number) => {
    const bounded = Math.max(1, Math.min(pageCount, next));
    setPage(bounded);
    updateSampleViewerUrl(runId, selectedColumns, bounded);
  };

  const initialLoading = (!run.data && run.loading) || (!samples.data && samples.loading);
  const fatalError = !run.data ? run.error : !samples.data ? samples.error : "";
  if (initialLoading || fatalError) {
    return <div className="comparison-viewer vela-focus-workspace" aria-busy={initialLoading}>
      <header className="viewer-topbar"><a href={`#/${returnTarget}${routeQuery({ project: run.data?.project_id })}`}>Return</a><a className="button" aria-label="Close sample viewer" href={`#/${returnTarget}${routeQuery({ project: run.data?.project_id })}`}><X size={16} /> Close</a></header>
      <main className="sample-comparison-boundary">
        <h1>Sample viewer</h1>
        <Notice loading={initialLoading} error={fatalError} />
        {fatalError && <div className="actions"><a className="button vela-button" href={`#/${returnTarget}${routeQuery({ project: run.data?.project_id })}`}>Back to {returnTarget.startsWith("model/") ? "model" : "run"}</a><button onClick={() => void Promise.all([run.reload(), samples.reload()])}>Retry</button></div>}
      </main>
    </div>;
  }

  return <div className="comparison-viewer vela-focus-workspace">
    <header className="viewer-topbar"><a href={`#/${returnTarget}${routeQuery({ project: run.data?.project_id })}`}>{str(run.data?.name, "Training run")}</a><div className="viewer-actions">{Boolean(run.data?.model_id) && <a className="button" href={`#/model/${str(run.data?.model_id)}${routeQuery({ project: run.data?.project_id })}`}>Model</a>}<a className="button" aria-label="Close sample viewer" href={`#/${returnTarget}${routeQuery({ project: run.data?.project_id })}`}><X size={16} /> Close</a></div></header>
    <main className="sample-comparison-workspace">
      <header className="comparison-toolbar">
        <div><h1>Sample comparison</h1><p>Prompt or sample rows by persisted checkpoint-step columns.</p></div>
        <span>{selected.length}/{MAX_SAMPLE_COLUMNS} selected</span>
      </header>
      <section className="sample-comparison-selector" aria-labelledby="sample-columns-heading">
        <div><h2 id="sample-columns-heading">Checkpoint columns</h2><p>Select up to {MAX_SAMPLE_COLUMNS}. Selection and page are stored in this URL.</p></div>
        <div className="chiplets">{columns.map(column => <button aria-pressed={selectedColumns.includes(column.id)} className={selectedColumns.includes(column.id) ? "active" : ""} key={column.id} onClick={() => selectColumn(column.id)}><strong>{column.label}</strong><span>{column.checkpointFilename || "Checkpoint record unavailable"}</span></button>)}</div>
        <p className="vela-meta" role="status" aria-live="polite">{selectionMessage}</p>
      </section>
      <Notice empty={!matrixRows.length} emptyText="No archived sample comparison" emptyHint="This run has no sample artifacts with prompt or checkpoint provenance." />
      {!!matrixRows.length && !selected.length && <div className="vela-empty"><span>Select a checkpoint column</span><small>Choose at least one real checkpoint step above to compare samples.</small></div>}
      {!!visibleRows.length && !!selected.length && <SampleComparisonTable columns={selected} rows={visibleRows} activeAssetId={activeAssetId} projectId={str(run.data?.project_id)} onSelect={setActiveAssetId} />}
      {!!visibleRows.length && !!selected.length && <SampleComparisonMobile columns={selected} rows={visibleRows} selectedColumnId={mobileColumn?.id ?? ""} selectedPromptId={mobileRow?.id ?? ""} sample={mobileSample} activeAssetId={activeAssetId} onColumn={setMobileColumnId} onPrompt={setMobilePromptId} onSelect={setActiveAssetId} />}
      {matrixRows.length > SAMPLE_ROWS_PER_PAGE && <nav className="sample-comparison-pagination" aria-label="Sample prompt pages"><button disabled={safePage <= 1} onClick={() => changePage(safePage - 1)}><ChevronLeft size={15} />Previous prompts</button><span>Prompts {(safePage - 1) * SAMPLE_ROWS_PER_PAGE + 1}–{Math.min(safePage * SAMPLE_ROWS_PER_PAGE, matrixRows.length)} of {matrixRows.length}</span><button disabled={safePage >= pageCount} onClick={() => changePage(safePage + 1)}>Next prompts<ChevronRight size={15} /></button></nav>}
      {activeSample && <SampleReviewPanel key={activeAssetId} sample={{ ...activeSample, project_id: activeSample.project_id ?? run.data?.project_id, origin_type: "SAMPLE" }} onSaved={samples.reload} />}
    </main>
  </div>;
}

export function forwardSampleComparisonWheel(event: { deltaX: number; deltaY: number; currentTarget: HTMLElement; preventDefault: () => void }) {
  if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || event.currentTarget.scrollHeight > event.currentTarget.clientHeight) return;
  event.preventDefault();
  window.scrollBy({ top: event.deltaY });
}

function SampleComparisonTable({ columns, rows: promptRows, activeAssetId, projectId, onSelect }: { columns: SampleComparisonColumn[]; rows: SampleComparisonRow[]; activeAssetId: string; projectId: string; onSelect: (assetId: string) => void }) {
  return <div className="sample-comparison-table-wrap" role="region" aria-label="Sample comparison matrix" tabIndex={0} onWheel={forwardSampleComparisonWheel}>
    <table className="sample-comparison-table">
      <thead><tr><th scope="col">Prompt / sample</th>{columns.map(column => <th scope="col" key={column.id}><strong>{column.label}</strong>{column.checkpointId ? <a href={`#/checkpoint/${column.checkpointId}${routeQuery({ project: projectId })}`}>{column.checkpointFilename || "Open checkpoint"}</a> : <span>{column.checkpointFilename || "Checkpoint unresolved"}</span>}<Status value={column.localStatus || column.checkpointState} />{column.modelVersionId ? <a href={`#/model-version/${column.modelVersionId}${routeQuery({ project: projectId })}`}>{column.modelVersionName || "Registered version"}</a> : <span>Not registered as a model version</span>}</th>)}</tr></thead>
      <tbody>{promptRows.map(row => <tr key={row.id}><th scope="row"><strong>{row.label}</strong>{row.ordinal != null && <span>Sample {row.ordinal + 1}</span>}</th>{columns.map(column => { const sample = row.cells[column.id]; const assetId = sample ? str(sample.asset_revision_id ?? sample.asset_id ?? sample.id) : ""; return <td key={column.id}>{sample ? <SampleCell sample={{ ...sample, project_id: sample.project_id ?? projectId }} active={activeAssetId === assetId} label={`${row.label} at ${column.label}`} onSelect={() => onSelect(assetId)} /> : <span className="sample-comparison-missing">Not archived</span>}</td>; })}</tr>)}</tbody>
    </table>
  </div>;
}

function SampleComparisonMobile({ columns, rows: promptRows, selectedColumnId, selectedPromptId, sample, activeAssetId, onColumn, onPrompt, onSelect }: { columns: SampleComparisonColumn[]; rows: SampleComparisonRow[]; selectedColumnId: string; selectedPromptId: string; sample: Row | null | undefined; activeAssetId: string; onColumn: (id: string) => void; onPrompt: (id: string) => void; onSelect: (id: string) => void }) {
  const assetId = sample ? str(sample.asset_revision_id ?? sample.asset_id ?? sample.id) : "";
  return <section className="sample-comparison-mobile" aria-label="Mobile sample detail">
    <div className="form-grid"><Field label="Prompt or sample"><Dropdown aria-label="Prompt or sample" value={selectedPromptId} onChange={onPrompt} options={promptRows.map(row => ({ value: row.id, label: row.label }))} /></Field><Field label="Checkpoint step"><Dropdown aria-label="Checkpoint step" value={selectedColumnId} onChange={onColumn} options={columns.map(column => ({ value: column.id, label: column.label }))} /></Field></div>
    {sample ? <SampleCell sample={sample} active={activeAssetId === assetId} label={`${str(sample.sample_label, "Sample")} at step ${str(sample.training_step, "unknown")}`} onSelect={() => onSelect(assetId)} /> : <div className="vela-empty"><span>Sample not archived</span><small>Choose another prompt or checkpoint step.</small></div>}
  </section>;
}

function SampleCell({ sample, active, label, onSelect }: { sample: Row; active: boolean; label: string; onSelect: () => void }) {
  const assetId = str(sample.asset_revision_id ?? sample.asset_id ?? sample.id);
  return <button type="button" className={`sample-comparison-cell${active ? " active" : ""}`} aria-pressed={active} aria-label={`Review ${label}`} onClick={onSelect}><AssetImage assetRevisionId={assetId} alt="" loading="lazy" /><span><Status value={sample.decision ?? "unreviewed"} />{sample.rating ? <small>{str(sample.rating)} / 5 rating</small> : <small>Not rated</small>}</span></button>;
}

function SampleReviewPanel({ sample, onSaved }: { sample: Row; onSaved: () => Promise<void> }) {
  const assetId = str(sample.asset_revision_id ?? sample.asset_id ?? sample.id);
  const [rating, setRating] = useState(Number(sample.rating ?? 0));
  const [decision, setDecision] = useState(str(sample.decision, "candidate"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<Row | null>(null);
  const reviewedBy = str(saved?.profile_name ?? sample.reviewed_by, "");
  const reviewedAt = saved?.created_at ?? sample.reviewed_at;
  const save = async () => {
    setBusy(true); setError(""); setSaved(null);
    try {
      const review = await api<Row>("/api/reviews", jsonBody({ subject_type: "asset", subject_id: assetId, rating: rating || null, decision }));
      setSaved(review);
      await onSaved();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setBusy(false); }
  };
  return <aside className="sample-review-panel" aria-labelledby="sample-review-heading">
    <header><div><h2 id="sample-review-heading">Review selected sample</h2><p>{str(sample.sample_label ?? sample.prompt ?? sample.name, "Training sample")} · Step {str(sample.training_step, "not recorded")}</p></div><a className="button vela-button" href={galleryOpenHref(sample)}>Open in Gallery</a></header>
    <div className="sample-review-layout"><AssetImage assetRevisionId={assetId} alt={str(sample.sample_label ?? sample.name, "Selected training sample")} loading="lazy" /><div className="sample-review-controls">
      <fieldset><legend>Rating</legend><div className="sample-rating-options">{[1, 2, 3, 4, 5].map(value => <label key={value}><input type="radio" name={`sample-rating-${assetId}`} value={value} checked={rating === value} onChange={() => setRating(value)} /><Star size={15} fill={rating >= value ? "currentColor" : "none"} /><span>{value}</span></label>)}</div></fieldset>
      <Field label="Production decision" hint="Decision is independent of the quality rating."><Dropdown aria-label="Production decision" value={decision} onChange={setDecision} options={[{ value: "candidate", label: "Candidate" }, { value: "approved", label: "Approved" }, { value: "hold", label: "Hold" }, { value: "reject", label: "Reject" }]} /></Field>
      <button className="vela-button vela-button-primary" disabled={busy} onClick={() => void save()}>{busy ? "Saving review..." : "Save review"}</button>
      {(saved || reviewedBy) && <p className="vela-notice" role="status" aria-live="polite"><strong>{saved ? "Review saved" : "Current review"}</strong><span>{decision} by {reviewedBy || "current operator"}{reviewedAt ? ` · ${new Date(String(reviewedAt)).toLocaleString()}` : ""}</span></p>}
      {error && <p className="vela-notice vela-notice-error" role="alert">{error}</p>}
    </div></div>
  </aside>;
}

function splitList(value: FormDataEntryValue | null) { return String(value ?? "").split(",").map(item => item.trim()).filter(Boolean); }
function initials(value: string) { return value.split(/\s+/).map(part => part[0]).join("").slice(0, 2).toUpperCase(); }
function formatDate(value: unknown) { const date = new Date(String(value ?? "")); return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
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
