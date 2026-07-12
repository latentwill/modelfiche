import { Fragment, useEffect, useRef, useState, type CSSProperties } from "react";
import { Chart, registerables, type ChartConfiguration } from "chart.js";
import { assembleChartjs } from "flint-chart/chartjs";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Check,
  Database,
  Download,
  ExternalLink,
  FileImage,
  FolderInput,
  Grid3X3,
  ImagePlus,
  Plus,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Maximize2,
  Minimize2,
  Star,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, patchBody, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Field, Form, Json, Notice, Page, Pagination, Panel, SelectRecords, Status } from "./ui";
import { generationReplayFromMetadata, generatorHref } from "./generation";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const formatBytes = (value: unknown) => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) { amount /= 1024; unit = units[index]; }
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${unit}`;
};
const scientificNotation = (value: unknown) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return str(value, "Unavailable");
  const scientific = number.toExponential().replace(/\.0+e/, "e").replace(/(\.\d*?[1-9])0+e/, "$1e").replace("e+", "e");
  return `${number} (${scientific})`;
};
type LossMetricPoint = { step: number; value: number; wall_time: number | null };
type LossMetrics = {
  run_id?: string;
  metric_name?: string;
  points?: unknown;
  final_value?: number | null;
  source_key?: string | null;
};

Chart.register(...registerables);

function finiteMetricNumber(value: unknown) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }
  return null;
}

function normalizeLossPoints(value: unknown): LossMetricPoint[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const point = item as Record<string, unknown>;
    const step = finiteMetricNumber(point.step);
    const metricValue = finiteMetricNumber(point.value);
    if (step === null || metricValue === null) return [];
    return [{ step, value: metricValue, wall_time: finiteMetricNumber(point.wall_time) }];
  }).sort((left, right) => left.step - right.step);
}

export function LossGraph({ points }: { points: unknown }) {
  const normalized = normalizeLossPoints(points);
  if (!normalized.length) {
    return <div className="loss-graph-empty" role="status"><strong>No loss metrics available</strong><small>The imported run artifacts did not include usable loss points.</small></div>;
  }

  return <FlintMetricChart points={normalized} label="Loss" color="#b64235" />;
}

function FlintMetricChart({ points, label, color }: { points: LossMetricPoint[]; label: string; color: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const context = canvas.current?.getContext?.("2d");
    if (!context || !points.length) return;
    const assembled = assembleChartjs({
      data: { values: points.map(point => ({ step: point.step, value: point.value })) },
      semantic_types: { step: "Quantity", value: "Quantity" },
      chart_spec: {
        chartType: "Line Chart",
        encodings: { x: { field: "step" }, y: { field: "value" } },
        baseSize: { width: 720, height: 260 },
      },
      options: { addTooltips: true },
    }) as ChartConfiguration;
    const dataset = assembled.data?.datasets?.[0];
    if (dataset) Object.assign(dataset, { label, borderColor: color, backgroundColor: `${color}22`, pointRadius: points.length > 300 ? 0 : 1.5, borderWidth: 2, tension: .18, fill: true });
    assembled.options = { ...assembled.options, responsive: true, maintainAspectRatio: false, animation: false };
    const chart = new Chart(context, assembled);
    return () => chart.destroy();
  }, [color, label, points]);
  return <div className="flint-metric-chart" role="img" aria-label={`${label} over training step`}><canvas ref={canvas} /></div>;
}


function ActionMessage({ message, error }: { message?: string; error?: string }) {
  if (error) return <div className="vela-notice vela-notice-error" role="alert"><strong>Action failed</strong><span>{error}</span></div>;
  if (message) return <div className="vela-notice" role="status" aria-live="polite"><strong>Updated</strong><span>{message}</span></div>;
  return null;
}

function ObjectImageStrip({ assets, label }: { assets: Row[]; label: string }) {
  if (!assets.length) return null;
  return <div className="object-image-strip" aria-label={label}>{assets.slice(0, 4).map(asset => <a href={galleryOpenHref(asset)} key={idOf(asset)}><AssetImage assetRevisionId={str(asset.asset_revision_id ?? asset.asset_id ?? asset.id)} alt={str(asset.name, label)} loading="lazy" /></a>)}</div>;
}

export function DatasetsScreen({ projectId = "" }: { projectId?: string }) {
  const resource = useResource<unknown>(`/api/datasets${query({ project_id: projectId })}`);
  const data = rows(resource.data);
  return <Page
    title="Datasets"
    subtitle="Versioned training images and captions"
    actions={<a className="button vela-button vela-button-primary" href="#/transfers?mode=import"><FolderInput size={15} />Import dataset</a>}
  >
    <Panel title="Dataset registry">
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />
      {!!data.length && <DataTable rows={data} columns={[["name", "Name"], ["project_name", "Project"], ["current_version", "Version"], ["item_count", "Items"], ["updated_at", "Updated"]]} onRow={row => { location.hash = `dataset/${idOf(row)}`; }} />}
    </Panel>
  </Page>;
}

export function DatasetScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/datasets/${id}`);
  const versionsResource = useResource<unknown>(`/api/datasets/${id}/versions`);
  const [draft, setDraft] = useState<Row | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [find, setFind] = useState("");
  const [replace, setReplace] = useState("");
  const [word, setWord] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<unknown>(null);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const data = resource.data;
  const versions = rows(data?.versions).length ? rows(data?.versions) : rows(versionsResource.data);
  const currentVersionId = str(data?.current_version_id ?? data?.version_id ?? versions[0]?.id, "");
  const activeVersionId = selectedVersionId || currentVersionId;
  const activeVersion = versions.find(version => idOf(version) === activeVersionId);
  const activeVersionReferenced = activeVersion?.status === "referenced";
  const publishedItems = useResource<unknown>(activeVersionId ? `/api/dataset-versions/${activeVersionId}/items?limit=1000` : null);
  const items = draft ? rows(draft.items) : rows(publishedItems.data);
  const visibleItems = items.slice((page - 1) * pageSize, page * pageSize);

  useEffect(() => setPage(1), [activeVersionId, draft?.id, pageSize]);

  async function createDraft() {
    if (!activeVersionId) return;
    setBusy("draft"); setActionError(""); setMessage("");
    try {
      const created = await api<Row>(`/api/datasets/${id}/drafts`, jsonBody({ base_version_id: activeVersionId }));
      setDraft(await api<Row>(`/api/dataset-drafts/${created.id}`));
      setMessage(`Working draft created from ${str(activeVersion?.name, "the selected version")}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function operation(dryRun: boolean, action: "replace" | "add_word" | "remove_word" | "set_included" = "replace", included?: boolean) {
    if (!draft?.id) return;
    setBusy(dryRun ? "preview" : action); setActionError(""); setMessage("");
    try {
      const parameters = action === "replace" ? { find, replace } : action === "set_included" ? { included: Boolean(included) } : { word };
      const payload = { operation: action, parameters, item_ids: selected.length ? selected : null, all: !selected.length };
      const result = await api(`/api/dataset-drafts/${draft.id}/operations${dryRun ? "/preview" : ""}`, jsonBody(payload));
      if (dryRun) setPreview(result);
      else {
        setDraft(await api<Row>(`/api/dataset-drafts/${draft.id}`));
        setPreview(null);
        setMessage(action === "add_word" ? "Word added to matching captions." : action === "remove_word" ? "Word removed from matching captions." : action === "set_included" ? `Matching images ${included ? "included" : "excluded"}.` : "Replacement applied to matching captions.");
      }
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function discardDraft() {
    if (!draft?.id || !window.confirm("Discard this working draft? Published versions will not be changed.")) return;
    setBusy("discard"); setActionError("");
    try {
      await api(`/api/dataset-drafts/${draft.id}`, { method: "DELETE" });
      setDraft(null); setSelected([]); setPreview(null); setMessage("Working draft discarded.");
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  const subtitle = `${str(data?.project_name, "Project dataset")} · ${items.length} images · ${draft ? "Working draft" : `v${str(activeVersion?.version_number ?? data?.current_version, "-")}`}`;
  return <Page title={str(data?.name, "Dataset")} subtitle={subtitle} actions={<>
    {activeVersionId ? <a className="button vela-button" href={`#/transfers?mode=export&dataset_version=${activeVersionId}`}><Download size={15} />Export version</a> : <button disabled title="Publish a dataset version before exporting"><Download size={15} />Export unavailable</button>}
    {!draft && <button className="vela-button vela-button-primary" onClick={() => void createDraft()} disabled={!activeVersionId || busy === "draft"} title={!activeVersionId ? "Import a dataset version before editing captions" : undefined}><Plus size={15} />{busy === "draft" ? "Creating..." : "New version"}</button>}
  </>}>
    <Notice error={resource.error || versionsResource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    <ObjectImageStrip assets={items} label="Dataset image preview" />

    <Panel title="Caption versions">
      <Notice loading={versionsResource.loading && !versions.length} empty={!versionsResource.loading && !versions.length} />
      {!!versions.length && <div className="caption-versions" aria-label="Dataset caption versions">
        {versions.map(version => {
          const versionId = idOf(version);
          const isActive = !draft && versionId === activeVersionId;
          const isCurrent = versionId === currentVersionId;
          return <button
            key={versionId}
            className={`caption-version ${isActive ? "active" : ""}`}
            aria-pressed={isActive}
            disabled={Boolean(draft)}
            title={draft ? "Publish or discard the working draft before changing the base version" : `View ${str(version.name, `version ${version.version_number}`)}`}
            onClick={() => { setSelectedVersionId(versionId); setSelected([]); setPreview(null); }}
          >
            <span className="caption-version-number">v{str(version.version_number)}</span>
            <span className="caption-version-body">
              <strong>{str(version.name, "Imported captions")}</strong>
              <small>{str(version.item_count, "0")} images · {str(version.caption_format, "text")} captions</small>
            </span>
            <span className={`caption-version-state ${isCurrent ? "current" : ""}`}>{isCurrent ? "Current" : str(version.status, "Published")}</span>
          </button>;
        })}
        {draft && <button className="caption-version active" aria-pressed="true" disabled title="The working copy is the active caption version"><span className="caption-version-number">Draft</span><span className="caption-version-body"><strong>Working copy</strong><small>{str(rows(draft.items).length, "0")} images · unpublished changes</small></span><span className="caption-version-state">Editing</span></button>}
      </div>}
    </Panel>
    {rows(data?.training_runs).length > 0 && <Panel title="Training runs using this dataset"><DataTable rows={rows(data?.training_runs)} columns={[["name", "Run"], ["status", "Status"], ["dataset_version_id", "Dataset version"]]} onRow={row => { location.hash = `run/${idOf(row)}`; }} /></Panel>}

    {draft && <Panel title="Batch caption operations">
      <div className="selection-toolbar">
        <strong>{selected.length ? `${selected.length} selected` : `All ${items.length} visible items`}</strong>
        <div className="actions">
          <button onClick={() => setSelected(items.map(item => idOf(item)).filter(Boolean))} disabled={!items.length || selected.length === items.length}><Check size={14} />Select all</button>
          <button onClick={() => setSelected([])} disabled={!selected.length}><X size={14} />Clear</button>
          <button onClick={() => void discardDraft()} disabled={busy === "discard"}><Trash2 size={14} />{busy === "discard" ? "Discarding..." : "Discard draft"}</button>
        </div>
      </div>
      <div className="form-grid">
        <Field label="Caption method" hint="Automated caption models are not connected yet."><select disabled title="Only manual caption editing is currently supported"><option>Manual edit</option></select></Field>
        <Field label="Caption format" hint="The imported version determines caption format."><select disabled title="Format conversion is not supported by this operation"><option>{str(activeVersion?.caption_format, "Text")}</option></select></Field>
        <Field label="Find"><input value={find} onChange={event => setFind(event.target.value)} placeholder="Text to replace" /></Field>
        <Field label="Replace with"><input value={replace} onChange={event => setReplace(event.target.value)} placeholder="Replacement text" /></Field>
        <Field label="Add word"><input value={word} onChange={event => setWord(event.target.value)} placeholder="Trigger or tag word" /></Field>
      </div>
      <div className="actions">
        <button onClick={() => void operation(true)} disabled={!find || Boolean(busy)} title={!find ? "Enter text to find before previewing" : undefined}><Search size={14} />{busy === "preview" ? "Previewing..." : "Preview replacement"}</button>
        <button className="primary" onClick={() => void operation(false)} disabled={!find || Boolean(busy)} title={!find ? "Enter text to find before replacing" : undefined}><Sparkles size={14} />{busy === "replace" ? "Replacing..." : "Apply replacement"}</button>
        <button onClick={() => void operation(false, "add_word")} disabled={!word || Boolean(busy)} title={!word ? "Enter a word before applying" : undefined}><Plus size={14} />{busy === "add_word" ? "Adding..." : "Add word"}</button>
        <button onClick={() => void operation(false, "remove_word")} disabled={!word || Boolean(busy)} title={!word ? "Enter a word before removing" : undefined}><X size={14} />{busy === "remove_word" ? "Removing..." : "Remove word"}</button>
        <button onClick={() => void operation(false, "set_included", true)} disabled={!selected.length || Boolean(busy)} title={!selected.length ? "Select images to include as a batch" : undefined}><Check size={14} />Include selected</button>
        <button onClick={() => void operation(false, "set_included", false)} disabled={!selected.length || Boolean(busy)} title={!selected.length ? "Select images to exclude as a batch" : undefined}><X size={14} />Exclude selected</button>
      </div>
      {preview !== null && <div className="vela-section"><div className="vela-section-heading"><h2>Operation preview</h2><button className="vela-text-button" onClick={() => setPreview(null)}>Dismiss</button></div><Json value={preview} /></div>}
      <Form submit="Publish version" onSubmit={async form => {
        await api(`/api/dataset-drafts/${draft.id}/publish`, jsonBody({ name: form.get("version_name") }));
        setDraft(null); setPreview(null); setSelected([]); setSelectedVersionId(""); setMessage("Dataset version published.");
        await Promise.all([resource.reload(), versionsResource.reload()]);
      }}><Field label="Version name" hint="Use a short label that explains the caption change."><input name="version_name" required placeholder="e.g. Trigger word cleanup" /></Field></Form>
    </Panel>}

    <Panel title="Images and captions">
      <Notice error={!draft ? publishedItems.error : undefined} loading={!draft && publishedItems.loading} empty={!publishedItems.loading && !items.length} />
      {!publishedItems.loading && !items.length && Boolean(data?.empty_reason) && <div className="vela-notice" role="status"><strong>Dataset is empty</strong><span>{str(data?.empty_reason)}</span></div>}
      {!publishedItems.loading && !items.length && activeVersionReferenced && <div className="vela-notice" role="status"><strong>Dataset files not indexed yet</strong><span>This version currently points to its training-storage path. Its images and caption files still need to be imported before captions can be reviewed.</span></div>}
      {!!items.length && <Pagination page={page} pageSize={pageSize} total={items.length} onPage={setPage} onPageSize={setPageSize} />}
      {!!visibleItems.length && <div className="caption-list">{visibleItems.map((item, index) => <CaptionItem key={idOf(item) || index} item={item} selected={selected.includes(idOf(item))} onSelect={checked => setSelected(checked ? [...selected, idOf(item)] : selected.filter(value => value !== idOf(item)))} draftId={str(draft?.id, "")} onSaved={created => {
        if (!draft) return;
        const next = [...items]; next[(page - 1) * pageSize + index] = { ...item, ...created }; setDraft({ ...draft, items: next });
      }} />)}</div>}
    </Panel>
  </Page>;
}

function CaptionItem({ item, draftId, selected, onSelect, onSaved }: { item: Row; draftId: string; selected: boolean; onSelect: (checked: boolean) => void; onSaved: (row: Row) => void }) {
  const [caption, setCaption] = useState(str(item.caption, ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const assetId = item.asset_revision_id ?? item.asset_id ?? item.id;
  const filename = str(item.filename ?? item.name, "Dataset image");
  useEffect(() => setCaption(str(item.caption, "")), [item.caption]);
  return <article className={`caption-item ${selected ? "selected" : ""}`}>
    <div className="caption-image">
      <label><input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} aria-label={`Select ${filename}`} /></label>
      <a href={galleryOpenHref(item, "dataset_image")} title={`Open ${filename} in the image reviewer`}><AssetImage assetRevisionId={String(assetId)} alt={filename} loading="lazy" /><span>Open image</span></a>
    </div>
    <div>
      <div className="caption-heading"><strong>{filename}</strong><Status value={item.caption_format ?? "text"} /></div>
      <textarea aria-label={`Caption for ${filename}`} value={caption} readOnly={!draftId} title={!draftId ? "Create a new version to edit this caption" : undefined} onChange={event => setCaption(event.target.value)} />
      {item.availability === "missing" && <div className="vela-notice vela-notice-error" role="alert">{str(item.availability_error, "The source image object is missing.")}</div>}
      <label title={!draftId ? "Create a new version to change inclusion" : undefined}><input type="checkbox" checked={Boolean(item.included ?? true)} disabled={!draftId || saving} onChange={async event => {
        if (!draftId) return;
        setSaving(true); setError("");
        try { onSaved(await api<Row>(`/api/dataset-drafts/${draftId}/items/${item.id}`, patchBody({ included: event.target.checked }))); }
        catch (reason) { setError(errorText(reason)); }
        finally { setSaving(false); }
      }} /> Included in this version</label>
      {error && <div className="vela-notice vela-notice-error" role="alert">{error}</div>}
      {draftId && <button onClick={async () => {
        setSaving(true); setError("");
        try { onSaved(await api<Row>(`/api/dataset-drafts/${draftId}/items/${item.id}`, patchBody({ caption }))); }
        catch (reason) { setError(errorText(reason)); }
        finally { setSaving(false); }
      }} disabled={saving}><Save size={14} />{saving ? "Saving..." : "Save caption"}</button>}
    </div>
  </article>;
}

export function RunsScreen({ projectId = "" }: { projectId?: string }) {
  const resource = useResource<unknown>(`/api/runs${query({ project_id: projectId })}`);
  const data = rows(resource.data);
  return <Page title="Training runs" subtitle="Imported trainer output, checkpoints, samples, and normalized configuration">
    <Panel title="Run registry">
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />
      {!!data.length && <DataTable rows={data} columns={[["name", "Run"], ["project_name", "Project"], ["trainer", "Trainer"], ["base_model", "Base model"], ["status", "Status"], ["checkpoint_count", "Checkpoints"]]} onRow={row => { location.hash = `run/${idOf(row)}`; }} />}
    </Panel>
  </Page>;
}

export function RunScreen({ id }: { id: string }) {
  const run = useResource<Row>(`/api/runs/${id}`);
  const samples = useResource<unknown>(`/api/runs/${id}/samples`);
  const checkpointsResource = useResource<unknown>(`/api/runs/${id}/checkpoints`);
  const config = useResource<Row>(`/api/runs/${id}/config`);
  const metrics = useResource<LossMetrics>(`/api/runs/${id}/metrics?name=loss`);
  const learningRateMetrics = useResource<LossMetrics>(`/api/runs/${id}/metrics?name=learning_rate`);
  const lineage = useResource<unknown>(`/api/lineage${query({ subject_type: "training_run", subject_id: id, depth: 3 })}`);
  const rawCheckpoints = rows(checkpointsResource.data);
  const enrichedCheckpoints = rows(run.data?.checkpoints);
  const checkpoints = enrichedCheckpoints.length
    ? enrichedCheckpoints.map(checkpoint => ({ ...rawCheckpoints.find(raw => idOf(raw) === idOf(checkpoint)), ...checkpoint }))
    : rawCheckpoints;
  const [selectedCheckpointId, setSelectedCheckpointId] = useState("");
  const selectedCheckpoint = checkpoints.find(checkpoint => idOf(checkpoint) === selectedCheckpointId) ?? null;
  const [datasetId, setDatasetId] = useState("");
  const [datasetLoading, setDatasetLoading] = useState(false);
  const [datasetError, setDatasetError] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const sampleRows = rows(samples.data);
  const sampleSteps = [...new Set(sampleRows.map(sample => str(sample.step, "unassigned")))];
  const datasetVersionId = str(run.data?.dataset_version_id, "");
  const runProjectId = str(run.data?.project_id, "");

  useEffect(() => {
    let active = true;
    setDatasetId(""); setDatasetError("");
    if (!datasetVersionId || !runProjectId) { setDatasetLoading(false); return () => { active = false; }; }
    setDatasetLoading(true);
    void (async () => {
      try {
        const datasets = rows(await api<unknown>(`/api/datasets${query({ project_id: runProjectId })}`));
        const current = datasets.find(dataset => str(dataset.current_version_id, "") === datasetVersionId);
        if (current) { if (active) setDatasetId(idOf(current)); return; }
        const versionCollections = await Promise.all(datasets.map(async dataset => ({ dataset, versions: rows(await api<unknown>(`/api/datasets/${idOf(dataset)}/versions`)) })));
        const parent = versionCollections.find(collection => collection.versions.some(version => idOf(version) === datasetVersionId));
        if (active) {
          if (parent) setDatasetId(idOf(parent.dataset));
          else setDatasetError("The linked dataset version is no longer available.");
        }
      } catch (reason) { if (active) setDatasetError(errorText(reason)); }
      finally { if (active) setDatasetLoading(false); }
    })();
    return () => { active = false; };
  }, [datasetVersionId, runProjectId]);

  async function refreshRun() {
    setBusy("refresh"); setMessage(""); setActionError("");
    try {
      const job = await api<Row>(`/api/runs/${id}/refresh`, jsonBody({}));
      await Promise.all([run.reload(), samples.reload(), checkpointsResource.reload(), metrics.reload(), learningRateMetrics.reload()]);
      setMessage(`Run refresh queued${job.id ? ` as job ${job.id}` : ""}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function hydrateCheckpoint(checkpoint: Row) {
    const checkpointId = idOf(checkpoint);
    setBusy(`hydrate:${checkpointId}`); setMessage(""); setActionError("");
    try {
      const job = await api<Row>(`/api/checkpoints/${checkpointId}/hydrate`, jsonBody({}));
      await Promise.all([run.reload(), checkpointsResource.reload()]);
      setMessage(`Checkpoint hydration queued${job.id ? ` as job ${job.id}` : ""}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  return <Page title={str(run.data?.name, "Training run")} subtitle={`${str(run.data?.base_model, "Unknown base")} · ${str(run.data?.dataset_version_name, "No dataset")} · ${str(run.data?.trainer, "Unknown trainer")}`} actions={<>
    {datasetId ? <a className="button vela-button" href={`#/dataset/${datasetId}`}><Database size={15} />Dataset</a> : <button disabled title={datasetError || (datasetLoading ? "Resolving the linked dataset version" : "This run does not reference an available dataset version")}><Database size={15} />{datasetLoading ? "Finding dataset..." : "Dataset unavailable"}</button>}
    <a className="button vela-button" href={`#/samples/${id}`}><FileImage size={15} />Samples</a>
    {run.data?.project_id ? <a className="button vela-button" href={`#/transfers?mode=export&project=${str(run.data.project_id)}`}><Download size={15} />Export project</a> : <button disabled title="This run is not attached to an exportable project"><Download size={15} />Export unavailable</button>}
    <button className="vela-button vela-button-primary" onClick={() => void refreshRun()} disabled={busy === "refresh"}><RefreshCw size={15} />{busy === "refresh" ? "Refreshing..." : "Refresh import"}</button>
  </>}>
    <Notice error={run.error} loading={run.loading} />
    <ActionMessage message={message} error={actionError} />
    <ObjectImageStrip assets={sampleRows.slice().reverse()} label="Latest training samples" />
    {run.data && <div className="run-summary vela-context-stats">
      <div className="vela-stat"><span>Status</span><Status value={run.data.status} /></div>
      <div className="vela-stat"><span>LoRA rank</span><strong>{str(run.data.lora_rank ?? run.data.network_dim, "-")}</strong></div>
      <div className="vela-stat"><span>Checkpoints</span><strong>{checkpoints.length}</strong></div>
      <div className="vela-stat"><span>Samples</span><strong>{sampleRows.length}</strong></div>
    </div>}
    {run.data?.source_status === "failed" && run.data?.usable_output === true && <div className="vela-notice" role="status"><strong>Usable training output recovered</strong><span>The trainer process reported “{str(run.data.source_message, "failed termination")}”, but the DAM found {checkpoints.length} usable checkpoints and retained the original termination details in normalized configuration.</span></div>}

    <Panel title="Training metrics">
      <Notice error={metrics.error || learningRateMetrics.error} loading={metrics.loading || learningRateMetrics.loading} />
      <div className="training-metric-summary"><div><span>Final loss</span><strong>{str(metrics.data?.final_value, "Unavailable")}</strong></div><div><span>Configured learning rate</span><strong>{scientificNotation(run.data?.learning_rate ?? (run.data?.normalized_config as Row | undefined)?.learning_rate)}</strong></div><div><span>Loss points</span><strong>{normalizeLossPoints(metrics.data?.points).length}</strong></div><div><span>LR points</span><strong>{normalizeLossPoints(learningRateMetrics.data?.points).length}</strong></div></div>
      <div className="training-chart-grid"><section><header><strong>Training loss</strong><small>Lower is generally better</small></header><LossGraph points={metrics.data?.points} /></section><section><header><strong>Learning rate</strong><small>Optimizer schedule</small></header>{normalizeLossPoints(learningRateMetrics.data?.points).length ? <FlintMetricChart points={normalizeLossPoints(learningRateMetrics.data?.points)} label="Learning rate" color="#315f72" /> : <div className="loss-graph-empty" role="status"><strong>No learning-rate series available</strong><small>The configured value remains visible above.</small></div>}</section></div>
    </Panel>

    <Panel title="Checkpoint timeline">
      <Notice error={!enrichedCheckpoints.length ? checkpointsResource.error : undefined} loading={checkpointsResource.loading && !enrichedCheckpoints.length} empty={!checkpointsResource.loading && !checkpoints.length} />
      {!!checkpoints.length && <div className="checkpoint-timeline">{checkpoints.map(checkpoint => { const sample = sampleRows.find(item => str(item.checkpoint_id) === idOf(checkpoint)) ?? sampleRows.find(item => str(item.step) === str(checkpoint.step)); return <button className={selectedCheckpointId === idOf(checkpoint) ? "active" : ""} aria-pressed={selectedCheckpointId === idOf(checkpoint)} key={idOf(checkpoint)} onClick={() => setSelectedCheckpointId(idOf(checkpoint))}>{sample && <AssetImage assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id)} alt="" loading="lazy" />}<strong>Step {str(checkpoint.step)}</strong><Status value={checkpoint.hydration_state ?? checkpoint.state} /><span>{str(checkpoint.filename ?? checkpoint.name)}</span></button>; })}</div>}
      {selectedCheckpoint && <div className="checkpoint-detail"><dl className="details"><dt>File</dt><dd>{str(selectedCheckpoint.filename ?? selectedCheckpoint.name)}</dd><dt>Size</dt><dd>{formatBytes(selectedCheckpoint.size)}</dd><dt>Storage</dt><dd>{str(selectedCheckpoint.hydration_state, "remote")}</dd><dt>Model version</dt><dd>{str(selectedCheckpoint.model_version_name, "Not registered")}</dd></dl><div className="actions"><a className="button vela-button" href={`#/checkpoint/${idOf(selectedCheckpoint)}`}><ExternalLink size={14} />Open checkpoint</a><button className="primary" onClick={() => void hydrateCheckpoint(selectedCheckpoint)} disabled={busy === `hydrate:${idOf(selectedCheckpoint)}`}><Download size={14} />{busy === `hydrate:${idOf(selectedCheckpoint)}` ? "Queuing..." : "Hydrate"}</button></div></div>}
    </Panel>

    <Panel title="Samples by checkpoint">
      <Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />
      {!samples.loading && !sampleRows.length && <div className="vela-notice" role="status"><strong>No sample images were archived</strong><span>The run can still contain valid checkpoints and metrics; its source prefix contains no sample-image artifacts to import.</span></div>}
      {!!sampleSteps.length && <div className="sample-step-strip">{sampleSteps.map(step => <a href={`#/samples/${id}?step=${encodeURIComponent(step)}`} key={step}><strong>Step {step}</strong><span>{sampleRows.filter(sample => str(sample.step, "unassigned") === step).length} images</span></a>)}</div>}
      {!!sampleRows.length && <AssetGrid assets={sampleRows.slice(0, 12)} />}
    </Panel>

    <div className="dashboard-lower vela-dashboard-grid">
      <Panel title="Normalized configuration"><Notice error={config.error} loading={config.loading} />{config.data && <ConfigDetails value={(config.data.normalized ?? config.data.normalized_config ?? config.data) as Row} datasetId={datasetId} />}</Panel>
      <Panel title="Training config"><Notice error={config.error} loading={config.loading} />{!config.loading && <details className="config-disclosure"><summary>Show raw training configuration</summary><Json value={config.data?.raw ?? config.data?.raw_config ?? run.data?.raw_config ?? (config.data?.normalized as Row | undefined)?.training_config ?? { state: "Not imported" }} /></details>}</Panel>
    </div>
    <Panel title="Lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} /><ObjectImageStrip assets={rows(lineage.data).filter(edge => edge.preview_asset_id).map(edge => ({ ...edge, id: edge.preview_asset_id, asset_revision_id: edge.preview_asset_id }))} label="Lineage image preview" />{!!rows(lineage.data).length && <DataTable rows={rows(lineage.data)} columns={[["source_type", "From"], ["relationship", "Relationship"], ["target_type", "To"], ["created_at", "Recorded"]]} />}</Panel>
  </Page>;
}

function ConfigDetails({ value, datasetId }: { value: Row; datasetId?: string }) {
  const entries = Object.entries(value).filter(([key]) => key !== "training_config");
  return <><dl className="details">{entries.slice(0, 14).map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{key === "learning_rate" ? scientificNotation(item) : key === "dataset_sources" && datasetId ? <a href={`#/dataset/${datasetId}`}>{Array.isArray(item) ? item.map(source => String(source).split("/").filter(Boolean).at(-1)).join(", ") : "Open dataset"}</a> : typeof item === "object" ? JSON.stringify(item) : str(item)}</dd></Fragment>)}</dl>{value.training_config && <details className="config-disclosure"><summary>Show complete training config</summary><Json value={value.training_config} /></details>}</>;
}

export function CheckpointScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/checkpoints/${id}`);
  const models = useResource<unknown>("/api/models");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [hydrating, setHydrating] = useState(false);
  const checkpointAsset = resource.data?.asset && typeof resource.data.asset === "object" ? resource.data.asset as Row : {};
  const artifact = resource.data?.artifact && typeof resource.data.artifact === "object" ? resource.data.artifact as Row : {};
  const storage = rows(artifact.storage);
  const checkpointProjectId = str(checkpointAsset.project_id, "");
  const modelRows = rows(models.data).filter(model => !checkpointProjectId || str(model.project_id, "") === checkpointProjectId);

  async function hydrate() {
    setHydrating(true); setActionError(""); setMessage("");
    try {
      const job = await api<Row>(`/api/checkpoints/${id}/hydrate`, jsonBody({}));
      await resource.reload();
      setMessage(`Hydration queued${job.id ? ` as job ${job.id}` : ""}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setHydrating(false); }
  }

  return <Page title={`Checkpoint ${str(resource.data?.step, "")}`} subtitle={str(resource.data?.run_name, "Training checkpoint")} actions={<button className="vela-button vela-button-primary" onClick={() => void hydrate()} disabled={hydrating || !resource.data} title={!resource.data ? "Checkpoint details must load before hydration" : undefined}><Download size={15} />{hydrating ? "Queuing..." : "Hydrate locally"}</button>}>
    <Notice error={resource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    {resource.data && <Panel title="Checkpoint artifact"><dl className="details"><dt>Filename</dt><dd>{str(artifact.filename, "Missing asset record")}</dd><dt>Checkpoint ID</dt><dd>{str(artifact.checkpoint_id)}</dd><dt>Asset ID</dt><dd>{str(artifact.asset_id)}</dd><dt>Revision</dt><dd>{str(artifact.checkpoint_revision_id, "No materialized revision")}</dd><dt>Step</dt><dd>{str(artifact.step)}</dd><dt>State</dt><dd><Status value={artifact.state} /></dd><dt>Size</dt><dd>{formatBytes(storage[0]?.size)}</dd><dt>Storage</dt><dd>{storage.length ? storage.map(location => `${str(location.provider)} · ${str(location.hydration_state)} · ${str(location.object_key ?? location.relative_path ?? location.uri)}`).join("; ") : "No storage location recorded"}</dd><dt>SHA-256</dt><dd>{str(artifact.sha256, "Not recorded")}</dd></dl><details className="config-disclosure"><summary>Artifact metadata</summary><Json value={artifact.metadata ?? {}} /></details></Panel>}
    <Panel title="Register model version">
      <Notice error={models.error} loading={models.loading} />
      {!models.loading && !modelRows.length && <div className="vela-empty"><span>No compatible model</span><small>Create a model in this checkpoint's project before registering a version.</small><a className="button vela-button" href="#/models"><Plus size={14} />Create model</a></div>}
      {!!modelRows.length && <Form submit="Register version" onSubmit={async form => {
        await api("/api/model-versions", jsonBody({
          checkpoint_id: id,
          model_id: form.get("model_id"),
          name: form.get("name"),
          lifecycle_state: "candidate",
          base_model: form.get("base_model") || null,
          trigger_words: String(form.get("trigger_words") ?? "").split(",").map(value => value.trim()).filter(Boolean),
          notes: form.get("notes"),
        }));
        setMessage("Candidate model version registered.");
        await resource.reload();
      }}><Field label="Model"><SelectRecords name="model_id" records={modelRows} required /></Field><Field label="Version name"><input name="name" required placeholder={`Step ${str(resource.data?.step, "checkpoint")}`} /></Field><Field label="Base model"><input name="base_model" placeholder="e.g. FLUX.1-dev" /></Field><Field label="Trigger words" hint="Separate multiple trigger words with commas."><input name="trigger_words" placeholder="subject_token, style_token" /></Field><Field label="Notes"><textarea name="notes" placeholder="Training observations or intended evaluation scope" /></Field></Form>}
    </Panel>
  </Page>;
}

export function ModelsScreen({ projectId = "" }: { projectId?: string }) {
  const resource = useResource<unknown>(`/api/models${query({ project_id: projectId })}`);
  const projects = useResource<unknown>("/api/projects");
  const data = rows(resource.data);
  const projectRows = rows(projects.data);
  return <Page title="Models" subtitle="Registered LoRA models and their checkpoint-backed versions">
    <Panel title="Create model">
      <Notice error={projects.error} loading={projects.loading} empty={!projects.loading && !projectRows.length} />
      {!!projectRows.length && <Form submit="Create model" onSubmit={async form => {
        await api("/api/models", jsonBody({ name: form.get("name"), project_id: form.get("project_id"), description: form.get("description") || null }));
        await resource.reload();
      }}><Field label="Project"><select name="project_id" required defaultValue={projectId}><option value="">Select...</option>{projectRows.map(project => <option key={idOf(project)} value={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field><Field label="Name"><input name="name" required placeholder="Model name" /></Field><Field label="Description"><textarea name="description" placeholder="Subject, style, and intended use" /></Field></Form>}
    </Panel>
    <Panel title="Model registry"><Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />{!!data.length && <DataTable rows={data} columns={[["name", "Name"], ["project_name", "Project"], ["base_model", "Base model"], ["latest_version", "Latest version"], ["lifecycle_state", "State"]]} onRow={row => { location.hash = `model/${idOf(row)}`; }} />}</Panel>
  </Page>;
}

export function ModelScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/models/${id}`);
  const versions = rows(resource.data?.versions);
  const projectId = str(resource.data?.project_id, "");
  const project = useResource<Row>(projectId ? `/api/projects/${projectId}` : null);
  const evals = useResource<unknown>(projectId ? `/api/eval-runs${query({ project_id: projectId })}` : null);
  const runs = useResource<unknown>(projectId ? `/api/runs${query({ project_id: projectId })}` : null);
  const preview = useResource<unknown>(`/api/gallery${query({ model_id: id, kind: "image", limit: 4 })}`);
  const [selectedVersion, setSelectedVersion] = useState<Row | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const selectedState = str(selectedVersion?.lifecycle_state, "").toLowerCase();
  const latestVersion = versions[0] ?? {};
  const versionRunIds = new Set(versions.flatMap(version => [str(version.checkpoint_run_id), str((version.readiness as Row | undefined)?.training_run_id)]).filter(Boolean));
  const projectRuns = rows(runs.data);
  const relatedRuns = projectRuns.filter(run => versionRunIds.has(idOf(run)));

  async function changeVersion(action: "approve" | "archive") {
    if (!selectedVersion) return;
    if (action === "archive" && !window.confirm(`Archive ${str(selectedVersion.name, "this model version")}?`)) return;
    setBusy(action); setMessage(""); setActionError("");
    try {
      await api(`/api/model-versions/${idOf(selectedVersion)}/${action}`, jsonBody({}));
      setMessage(action === "approve" ? "Model version approved." : "Model version archived.");
      setSelectedVersion(null);
      await resource.reload();
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  return <Page title={str(resource.data?.name, "Model")} subtitle={`${str(latestVersion.base_model, "Base model unknown")} · ${str(project.data?.title ?? project.data?.name, "Project")}`} actions={<>
    {projectId ? <><a className="button vela-button vela-button-primary" href={generatorHref("image", { kind: "model", projectId, modelId: id, modelVersionId: idOf(latestVersion) })}><ImagePlus size={15} /> + Image</a><a className="button vela-button" href={`#/evals?project=${projectId}&scope=model&model=${id}&model_version=${idOf(latestVersion)}`}><Sparkles size={15} /> Eval</a><a className="button vela-button" href={`#/grids?project=${projectId}&scope=model&model=${id}&model_version=${idOf(latestVersion)}`}><Grid3X3 size={15} /> Grid</a></> : <button disabled title="The model is not attached to a project">Generation unavailable</button>}
    {idOf(latestVersion) ? <a className="button vela-button" href={`#/transfers?mode=export&model_version=${idOf(latestVersion)}`}><Download size={15} />Export version</a> : <button disabled title="Register a model version before exporting"><Download size={15} />Export unavailable</button>}
  </>}>
    <Notice error={resource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    <ObjectImageStrip assets={rows(preview.data)} label="Latest model outputs" />
    {resource.data && <Panel title="Model record"><Notice error={project.error} loading={project.loading} /><dl className="details"><dt>Description</dt><dd>{str(resource.data.description, "No description")}</dd><dt>Latest trigger words</dt><dd>{Array.isArray(latestVersion.trigger_words) && latestVersion.trigger_words.length ? latestVersion.trigger_words.join(", ") : "None registered"}</dd><dt>Base model</dt><dd>{str(latestVersion.base_model, "Not registered")}</dd><dt>Project</dt><dd>{projectId ? <a href={`#/project/${projectId}`}>{str(project.data?.title ?? project.data?.name, "Open project")}</a> : "Not assigned"}</dd><dt>Versions</dt><dd>{versions.length}</dd></dl></Panel>}

    <Panel title="Eval Runs">
      <Notice error={evals.error} loading={evals.loading} empty={!evals.loading && !rows(evals.data).length} />
      {!!rows(evals.data).length && <div className="eval-run-strip">{rows(evals.data).map(evaluation => <a href={`#/eval/${idOf(evaluation)}`} key={idOf(evaluation)}><div className="eval-thumbs">{rows(evaluation.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={String(output.asset_revision_id ?? output.asset_id)} alt="Eval output" />)}</div><strong>{str(evaluation.name, "Eval run")}</strong><span>{str(evaluation.prompt_set_name)} · {str(evaluation.output_count, "0")} outputs</span><Status value={evaluation.status} /></a>)}</div>}
    </Panel>

    <Panel title="Model versions">
      <Notice loading={resource.loading} empty={!resource.loading && !versions.length} />
      {!!versions.length && <DataTable rows={versions} columns={[["filename", "Checkpoint file"], ["asset_id", "Artifact ID"], ["checkpoint_step", "Step"], ["lifecycle_state", "State"], ["fal_status", "FAL upload"], ["fal_url", "FAL URL / identifier"]]} onRow={setSelectedVersion} />}
      {selectedVersion && <div className="actions version-actions" aria-label={`Actions for ${str(selectedVersion.name, "selected version")}`}>
        <strong>{str(selectedVersion.name, "Selected version")}</strong>
        <button className="primary" onClick={() => void changeVersion("approve")} disabled={selectedState === "approved" || Boolean(busy)} title={selectedState === "approved" ? "This version is already approved" : undefined}><Check size={14} />{busy === "approve" ? "Approving..." : "Approve"}</button>
        <button onClick={() => void changeVersion("archive")} disabled={selectedState === "archived" || Boolean(busy)} title={selectedState === "archived" ? "This version is already archived" : undefined}><Archive size={14} />{busy === "archive" ? "Archiving..." : "Archive"}</button>
        <button aria-label="Clear selected model version" title="Clear selection" onClick={() => setSelectedVersion(null)}><X size={14} /></button>
      </div>}
    </Panel>

    <Panel title="Training run data">
      <Notice error={runs.error} loading={runs.loading} empty={!runs.loading && !relatedRuns.length} />
      {relatedRuns.map(run => <ModelTrainingRun key={idOf(run)} run={run} />)}
    </Panel>
  </Page>;
}

function ModelTrainingRun({ run }: { run: Row }) {
  const [expanded, setExpanded] = useState(true);
  const runId = idOf(run);
  const samples = useResource<unknown>(expanded ? `/api/runs/${runId}/samples` : null);
  const metrics = useResource<LossMetrics>(expanded ? `/api/runs/${runId}/metrics?name=loss` : null);
  const config = useResource<Row>(expanded ? `/api/runs/${runId}/config` : null);
  const sampleRows = rows(samples.data);
  return <details className="training-run" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><strong>{str(run.name, "Training run")}</strong><span>{str(run.dataset_version_name, "No linked dataset")}</span><Status value={run.status} /></summary>
    <div className="training-run-body">
      <dl className="details"><dt>Base</dt><dd>{str(run.base_model)}</dd><dt>Checkpoints</dt><dd>{str(run.checkpoint_count, "0")}</dd><dt>Samples</dt><dd>{str(run.sample_count, "0")}</dd><dt>Dataset</dt><dd>{str(run.dataset_version_name, "Not linked")}</dd></dl>
      <div className="actions"><a className="button vela-button" href={`#/run/${runId}`}><ExternalLink size={14} />Open run and loss graph</a><a className="button vela-button" href={`#/samples/${runId}`}><FileImage size={14} />View all samples</a></div>
      {expanded && <>
        <section className="vela-section"><div className="vela-section-heading"><h3>Sample images</h3></div><Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />{!!sampleRows.length && <AssetGrid assets={sampleRows.slice(0, 8)} />}</section>
        <section className="vela-section"><div className="vela-section-heading"><h3>Loss graph</h3></div><Notice error={metrics.error} loading={metrics.loading} /><LossGraph points={metrics.data?.points} /></section>
        <section className="vela-section"><div className="vela-section-heading"><h3>Training configuration</h3></div><Notice error={config.error} loading={config.loading} />{config.data && <details className="config-disclosure"><summary>Show stored training configuration</summary><ModelConfigDetails value={(config.data.normalized ?? {}) as Row} raw={(config.data.raw ?? {}) as Row} datasetName={str(run.dataset_version_name, "Not linked")} /></details>}</section>
      </>}
    </div>
  </details>;
}

function ModelConfigDetails({ value, raw, datasetName }: { value: Row; raw: Row; datasetName: string }) {
  const aliases: Array<[string, string[]]> = [
    ["Dataset", ["dataset", "dataset_name", "dataset_sources"]],
    ["Learning rate", ["learning_rate"]],
    ["Rank", ["lora_rank", "rank", "network_dim"]],
    ["Steps", ["steps", "max_train_steps"]],
    ["Scheduler", ["scheduler", "lr_scheduler"]],
    ["Optimizer", ["optimizer", "optimizer_type"]],
    ["Resolution", ["resolution"]],
    ["Batch", ["batch", "batch_size", "train_batch_size"]],
    ["Seed", ["seed"]],
    ["Base model", ["base_model", "pretrained_model_name_or_path"]],
  ];
  const used = new Set<string>();
  const primary = aliases.map(([label, keys]) => {
    const key = keys.find(candidate => Object.prototype.hasOwnProperty.call(value, candidate));
    if (key) used.add(key);
    return [label, key ? value[key] : label === "Dataset" ? datasetName : "Not recorded"] as const;
  });
  const other = Object.entries(value).filter(([key]) => !used.has(key) && key !== "training_config");
  const display = (label: string, item: unknown) => label === "Learning rate" ? scientificNotation(item) : typeof item === "object" ? JSON.stringify(item) : str(item);
  return <><dl className="details">{primary.map(([label, item]) => <Fragment key={label}><dt>{label}</dt><dd>{display(label, item)}</dd></Fragment>)}{other.map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{display(key, item)}</dd></Fragment>)}</dl>{value.training_config ? <><h4>Nested training config</h4><Json value={value.training_config} /></> : null}<h4>Imported source config</h4><Json value={raw} /></>;
}

type GalleryFilters = {
  project_id: string;
  model_id: string;
  dataset_id: string;
  eval_run_id?: string;
  category: string;
  kind: string;
  decision: string;
  rating: string;
  q: string;
  include_dataset_assets: boolean;
  sort: string;
};

function galleryFiltersFromParams(params: URLSearchParams, projectId: string, defaults?: GalleryFilters): Partial<GalleryFilters> {
  const isExplicitGalleryScope = ["project", "model", "dataset", "eval", "category", "kind", "decision", "rating", "q", "sort", "include_dataset_assets", "asset", "return"].some(key => params.has(key));
  if (isExplicitGalleryScope && defaults) {
    return {
      ...defaults,
      project_id: params.get("project") ?? projectId,
      model_id: params.get("model") ?? "",
      dataset_id: params.get("dataset") ?? "",
      eval_run_id: params.get("eval") ?? "",
      category: params.get("category") ?? "",
      kind: params.get("kind") ?? "image",
      decision: params.get("decision") ?? "",
      rating: params.get("rating") ?? "",
      q: params.get("q") ?? "",
      sort: params.get("sort") ?? "origin_time_desc",
      include_dataset_assets: params.has("include_dataset_assets") ? params.get("include_dataset_assets") === "true" : params.get("category") === "dataset_image",
    };
  }
  const filters: Partial<GalleryFilters> = {};
  const project = params.get("project") ?? projectId;
  if (project) filters.project_id = project;
  if (params.has("model")) filters.model_id = params.get("model") ?? "";
  if (params.has("dataset")) filters.dataset_id = params.get("dataset") ?? "";
  if (params.has("eval")) filters.eval_run_id = params.get("eval") ?? "";
  if (params.has("category")) filters.category = params.get("category") ?? "";
  if (params.has("kind")) filters.kind = params.get("kind") ?? "image";
  if (params.has("decision")) filters.decision = params.get("decision") ?? "";
  if (params.has("rating")) filters.rating = params.get("rating") ?? "";
  if (params.has("q")) filters.q = params.get("q") ?? "";
  if (params.has("sort")) filters.sort = params.get("sort") ?? "origin_time_desc";
  if (params.has("include_dataset_assets")) filters.include_dataset_assets = params.get("include_dataset_assets") === "true";
  return filters;
}

export function safeReturnTarget(value: string | null) {
  if (!value || value.startsWith("/") || value.startsWith("#") || /^[a-z]+:/i.test(value)) return "";
  return value;
}

export function galleryHash(filters: GalleryFilters, page: number, pageSize: number, assetId = "", returnTarget = "") {
  return `#/gallery${query({
    project: filters.project_id,
    model: filters.model_id,
    dataset: filters.dataset_id,
    eval: filters.eval_run_id,
    category: filters.category,
    kind: filters.kind === "image" ? "" : filters.kind,
    decision: filters.decision,
    rating: filters.rating,
    q: filters.q,
    sort: filters.sort === "origin_time_desc" ? "" : filters.sort,
    include_dataset_assets: filters.include_dataset_assets ? true : "",
    page,
    pageSize,
    asset: assetId,
    return: returnTarget,
  })}`;
}

export function galleryOpenHref(asset: Row, category = "") {
  const metadata = asset.metadata && typeof asset.metadata === "object" ? asset.metadata as Row : {};
  const origin = str(asset.origin_type ?? metadata.origin_type, "").toUpperCase();
  const resolvedCategory = category || (origin === "EVAL" ? "eval_output" : origin === "SAMPLE" ? "sample" : origin === "DATASET" ? "dataset_image" : "");
  const returnTarget = typeof location !== "undefined" ? safeReturnTarget(location.hash.replace(/^#\/?/, "")) : "";
  return `#/gallery${query({ project: asset.project_id ?? metadata.project_id, eval: asset.eval_run_id ?? metadata.eval_run_id, category: resolvedCategory, include_dataset_assets: resolvedCategory === "dataset_image" ? true : "", asset: asset.asset_revision_id ?? asset.asset_id ?? asset.id, return: returnTarget })}`;
}

export function isGalleryShortcutTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest("input, textarea, select, [contenteditable=true]"));
}

export function GalleryScreen({ projectId = "", params = new URLSearchParams() }: { projectId?: string; params?: URLSearchParams }) {
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [page, setPage] = useState(() => Math.max(1, Number(params.get("page")) || Number(localStorage.getItem("titles.gallery.page")) || 1));
  const [pageSize, setPageSize] = useState(() => Number(params.get("pageSize")) || Number(localStorage.getItem("titles.gallery.pageSize")) || 50);
  const defaultFilters: GalleryFilters = { project_id: projectId, model_id: "", dataset_id: "", eval_run_id: "", category: "", kind: "image", decision: "", rating: "", q: "", include_dataset_assets: false, sort: "origin_time_desc" };
  const [filters, setFilters] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("titles.gallery.filters") ?? "null") as Partial<typeof defaultFilters> | null;
      const routeFilters = galleryFiltersFromParams(params, projectId, defaultFilters);
      return { ...defaultFilters, ...(saved ?? {}), ...routeFilters };
    } catch { return defaultFilters; }
  });
  const models = useResource<unknown>(`/api/models${query({ project_id: filters.project_id })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: filters.project_id })}`);
  const [size, setSize] = useState(170);
  const [actionNotice, setActionNotice] = useState("");
  const path = `/api/gallery${query({ ...filters, paginated: true, limit: pageSize, offset: (page - 1) * pageSize })}`;
  const resource = useResource<Row>(path);
  const assets = rows(resource.data);
  const total = Number(resource.data?.total ?? assets.length);
  const isFiltered = Object.entries(filters).some(([key, value]) => value !== defaultFilters[key as keyof typeof defaultFilters]);
  const resetKey = JSON.stringify([filters, pageSize]);
  const previousResetKey = useRef(resetKey);
  const selectedAssetId = params.get("asset") ?? "";
  const returnTarget = safeReturnTarget(params.get("return"));
  const closeHref = returnTarget ? `#/${returnTarget}` : galleryHash(filters, page, pageSize);
  const routeSignature = params.toString();
  useEffect(() => {
    const nextPage = Number(params.get("page"));
    const nextPageSize = Number(params.get("pageSize"));
    if (Number.isFinite(nextPage) && nextPage > 0) setPage(nextPage);
    if (Number.isFinite(nextPageSize) && nextPageSize > 0) setPageSize(nextPageSize);
    const routeFilters = galleryFiltersFromParams(params, projectId, defaultFilters);
    if (Object.keys(routeFilters).length) setFilters(current => ({ ...current, ...routeFilters }));
  }, [routeSignature, projectId]);
  useEffect(() => {
    if (previousResetKey.current !== resetKey) {
      previousResetKey.current = resetKey;
      setPage(1);
    }
  }, [resetKey]);
  useEffect(() => { localStorage.setItem("titles.gallery.filters", JSON.stringify(filters)); }, [filters]);
  useEffect(() => { localStorage.setItem("titles.gallery.pageSize", String(pageSize)); }, [pageSize]);
  useEffect(() => { localStorage.setItem("titles.gallery.page", String(page)); }, [page]);
  return <Page title="Gallery" subtitle="Training samples, evals, and generated assets; dataset members are opt-in" actions={<a className="button vela-button vela-button-primary" href="#/transfers?mode=import"><Upload size={15} />Import assets</a>}>
    <Panel title="Filters">
      <Notice error={projects.error} loading={projects.loading} />
      <div className="form-grid">
        <Field label="Project"><select value={filters.project_id} onChange={event => setFilters({ ...filters, project_id: event.target.value, model_id: "", dataset_id: "" })}><option value="">All projects</option>{rows(projects.data).map(project => <option key={idOf(project)} value={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>
        <Field label="Model"><select value={filters.model_id} onChange={event => setFilters({ ...filters, model_id: event.target.value })}><option value="">All models</option>{rows(models.data).map(model => <option key={idOf(model)} value={idOf(model)}>{str(model.name)}</option>)}</select></Field>
        <Field label="Dataset"><select value={filters.dataset_id} onChange={event => setFilters({ ...filters, dataset_id: event.target.value })}><option value="">All datasets</option>{rows(datasets.data).map(dataset => <option key={idOf(dataset)} value={idOf(dataset)}>{str(dataset.name)}</option>)}</select></Field>
        <Field label="Image set"><select value={filters.category} onChange={event => setFilters({ ...filters, category: event.target.value, include_dataset_assets: event.target.value === "dataset_image" ? true : filters.include_dataset_assets })}><option value="">All image sets</option><option value="eval_output">Eval outputs</option><option value="sample">Training samples</option><option value="dataset_image">Dataset images</option></select></Field>
        <Field label="Search"><input value={filters.q} onChange={event => setFilters({ ...filters, q: event.target.value })} placeholder="Filename" /></Field>
        <Field label="Decision"><select value={filters.decision} onChange={event => setFilters({ ...filters, decision: event.target.value })}><option value="">Any</option><option value="candidate">Candidate</option><option value="approved">Approved</option><option value="hold">Hold</option><option value="reject">Reject</option></select></Field>
        <Field label="Rating"><select value={filters.rating} onChange={event => setFilters({ ...filters, rating: event.target.value })}><option value="">Any</option>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value} star{value === 1 ? "" : "s"}</option>)}</select></Field>
        <Field label="Sort"><select value={filters.sort} onChange={event => setFilters({ ...filters, sort: event.target.value })}><option value="origin_time_desc">Newest generated or modified</option><option value="origin_time_asc">Oldest generated or modified</option></select></Field>
        <Field label="Dataset images"><label><input type="checkbox" checked={filters.include_dataset_assets} onChange={event => setFilters({ ...filters, include_dataset_assets: event.target.checked })} /> Include dataset members</label></Field>
        <Field label={`Thumbnail size · ${size}px`}><input aria-label="Thumbnail size" type="range" min="120" max="260" value={size} onChange={event => setSize(Number(event.target.value))} /></Field>
      </div>
      <div className="actions"><button onClick={() => setFilters(defaultFilters)} disabled={!isFiltered} title={!isFiltered ? "No additional filters are active" : undefined}><X size={14} />Clear filters</button><span className="vela-meta" aria-live="polite">{total} matching assets</span></div>
    </Panel>
    <Panel title="Assets">
      <ActionMessage message={actionNotice} />
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !assets.length} />
      {!!assets.length && <div style={{ "--asset-size": `${size}px` } as CSSProperties}><AssetGrid assets={assets} hrefForAsset={asset => galleryHash(filters, page, pageSize, str(asset.asset_revision_id ?? asset.asset_id ?? asset.id), returnTarget)} /></div>}
      {total > 0 && <Pagination page={page} pageSize={pageSize} total={total} onPage={setPage} onPageSize={setPageSize} />}
    </Panel>
    {selectedAssetId && <GalleryImageOverlay id={selectedAssetId} items={assets} filters={filters} page={page} pageSize={pageSize} closeHref={closeHref} returnTarget={returnTarget} onDeleted={resource.reload} onDeleteNotice={setActionNotice} />}
  </Page>;
}

function GalleryImageOverlay({ id, items, filters, page, pageSize, closeHref, returnTarget, onDeleted, onDeleteNotice }: { id: string; items: Row[]; filters: GalleryFilters; page: number; pageSize: number; closeHref: string; returnTarget: string; onDeleted: () => Promise<void>; onDeleteNotice: (message: string) => void }) {
  const asset = useResource<Row>(`/api/assets/${id}`);
  const context = useResource<Row>(`/api/assets/${id}/context`);
  const galleryContext = useResource<Row>(`/api/gallery/context${query({ ...filters, asset_id: id })}`);
  const comments = useResource<unknown>(`/api/comments${query({ subject_type: "asset", subject_id: id })}`);
  const reviews = useResource<unknown>(`/api/reviews${query({ subject_type: "asset", subject_id: id })}`);
  const overlay = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [ratingBusy, setRatingBusy] = useState(false);
  const [ratingError, setRatingError] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const contextItems = rows(galleryContext.data?.items);
  const navigationItems = contextItems.length ? contextItems : items;
  const index = navigationItems.findIndex(item => idOf(item) === id || str(item.asset_revision_id ?? item.asset_id) === id);
  const previous = (galleryContext.data?.previous as Row | undefined) ?? (index > 0 ? navigationItems[index - 1] : undefined);
  const next = (galleryContext.data?.next as Row | undefined) ?? (index >= 0 ? navigationItems[index + 1] : undefined);
  const currentRating = Number(rows(reviews.data)[0]?.rating ?? 0);
  const metadata = context.data?.metadata && typeof context.data.metadata === "object" ? context.data.metadata as Row : {};
  const hrefFor = (item: Row) => galleryHash(filters, page, pageSize, str(item.asset_revision_id ?? item.asset_id ?? item.id), returnTarget);
  const rate = async (value: number) => { setRatingBusy(true); setRatingError(""); try { await api("/api/reviews", jsonBody({ subject_type: "asset", subject_id: id, rating: value, decision: "candidate" })); await reviews.reload(); } catch (reason) { setRatingError(errorText(reason)); } finally { setRatingBusy(false); } };
  useEffect(() => {
    const changed = () => setFullscreen(document.fullscreenElement === overlay.current);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (isGalleryShortcutTarget(event.target)) return;
      if (event.key === "ArrowLeft" && previous) { event.preventDefault(); location.hash = hrefFor(previous).slice(1); }
      if (event.key === "ArrowRight" && next) { event.preventDefault(); location.hash = hrefFor(next).slice(1); }
      if (/^[1-5]$/.test(event.key) && !ratingBusy) { event.preventDefault(); void rate(Number(event.key)); }
      if (event.key === "Escape" && !document.fullscreenElement) location.hash = closeHref.slice(1);
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [id, previous && idOf(previous), next && idOf(next), ratingBusy, closeHref]);
  const originType = str(metadata.origin_type ?? (galleryContext.data?.current ? (galleryContext.data.current as Row).origin_type : undefined), "ASSET");
  const prompt = str(metadata.prompt ?? metadata.caption, "");
  const replay = generationReplayFromMetadata({ ...metadata, project_id: metadata.project_id ?? asset.data?.project_id });
  return <div ref={overlay} className={`review-overlay vela-focus-workspace gallery-review-overlay ${fullscreen ? "is-fullscreen" : ""}`}><header className="viewer-topbar"><a href={closeHref}>Gallery</a><div className="viewer-actions"><button title={fullscreen ? "Exit full screen" : "Use full screen"} aria-label={fullscreen ? "Exit Full Screen" : "Full Screen"} onClick={() => { if (document.fullscreenElement) void document.exitFullscreen(); else void overlay.current?.requestFullscreen(); }}>{fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}<span>{fullscreen ? "Exit Full Screen" : "Full Screen"}</span></button><a className="button" aria-label="Close image review" href={closeHref}><X size={16} /> Close</a></div></header>
    <main className="image-review-stage"><div className="image-review-main"><div className="image-nav"><a aria-disabled={!previous} href={previous ? hrefFor(previous) : undefined}><ChevronLeft size={16} /> Previous</a><strong>{str(asset.data?.name, "Image")}</strong><a aria-disabled={!next} href={next ? hrefFor(next) : undefined}>Next <ChevronRight size={16} /></a></div><div className="image-review-media"><span className={`asset-origin-badge origin-${originType.toLowerCase()}`}>{originType}</span><AssetImage className="fit" assetRevisionId={id} variant="content" alt={str(asset.data?.name, "Selected image")} /></div>{prompt && <p className="image-review-prompt">{prompt}</p>}
      <div className="filmstrip">{navigationItems.slice(Math.max(0, index - 4), index + 5).map(item => <a className={idOf(item) === id ? "active" : ""} href={hrefFor(item)} key={idOf(item)}><AssetImage assetRevisionId={idOf(item)} alt={str(item.name)} /></a>)}</div></div>
      <aside id="storage_details" className="image-review-inspector"><h2>Image review</h2><Notice error={asset.error || context.error || galleryContext.error} loading={asset.loading || context.loading || galleryContext.loading} /><dl className="details"><dt>File</dt><dd>{str(asset.data?.name)}</dd><dt>Type</dt><dd>{str(asset.data?.mime_type)}</dd><dt>Kind</dt><dd>{str(asset.data?.kind)}</dd><dt>Dimensions</dt><dd>{str(metadata.width ?? (context.data?.image as Row | undefined)?.width)} x {str(metadata.height ?? (context.data?.image as Row | undefined)?.height)}</dd><dt>Model</dt><dd>{str(metadata.model_name, "Unknown")}</dd><dt>Checkpoint</dt><dd>{str(metadata.model_version_name, "Unknown")}</dd><dt>Base model</dt><dd>{str(metadata.base_model, "Unknown")}</dd></dl>
        {replay && <a className="button vela-button" href={generatorHref("image", replay.context, id)}><ImagePlus size={15} /> Generate with these settings</a>}
        <div className="rating-control"><span>Image rating (keys 1–5)</span>{[1,2,3,4,5].map(value => <button className="vela-rating-button" aria-label={`${value} stars`} aria-pressed={currentRating === value} disabled={ratingBusy} key={value} onClick={() => void rate(value)}><Star size={16} fill={currentRating >= value ? "currentColor" : "none"} /></button>)}</div>{ratingError && <div className="vela-notice vela-notice-error" role="alert">{ratingError}</div>}
        <div className="image-delete-control"><button className="vela-button vela-button-danger" disabled={deleteBusy} onClick={async () => { if (!confirm(`Delete ${str(asset.data?.name, "this image")}? This removes DAM metadata and supported local content.`)) return; setDeleteBusy(true); setDeleteError(""); try { const result = await api<Row>(`/api/assets/${id}?report=true&delete_content=true`, { method: "DELETE" }); const retained = rows(result.storage).filter(item => item.status !== "deleted"); onDeleteNotice(retained.length ? `${retained.length} storage object(s) retained; DAM metadata deleted.` : "Image and local content deleted."); const destination = next ? hrefFor(next) : previous ? hrefFor(previous) : closeHref; await onDeleted(); location.hash = destination.slice(1); } catch (reason) { setDeleteError(errorText(reason)); } finally { setDeleteBusy(false); } }}><Trash2 size={15} />{deleteBusy ? "Deleting…" : "Delete image"}</button>{deleteError && <div className="vela-notice vela-notice-error" role="alert">{deleteError}</div>}</div>
        <section><h3>Comments</h3><div className="comments">{rows(comments.data).map(comment => <article key={idOf(comment)}><strong>{str(comment.profile_name, "Operator")}</strong><p>{str(comment.body)}</p></article>)}</div><Form submit="Add comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: "asset", subject_id: id, body: form.get("body") })); await comments.reload(); }}><Field label="Comment"><textarea name="body" required /></Field></Form></section>
      </aside></main>
  </div>;
}

export function AssetGrid({ assets, hrefForAsset }: { assets: Row[]; hrefForAsset?: (asset: Row) => string }) {
  return <div className="asset-grid">{assets.map((asset, index) => {
    const name = str(asset.filename ?? asset.name, "Generated image");
    const metadata = asset.metadata && typeof asset.metadata === "object" ? asset.metadata as Row : {};
    const step = asset.step ?? metadata.step;
    const seed = asset.seed ?? metadata.seed;
    const model = str(asset.model_name ?? metadata.model_name, "");
    const checkpoint = str(metadata.model_version_name, "");
    const baseModel = str(asset.base_model ?? metadata.base_model, "");
    const dataset = str(asset.dataset_name ?? metadata.dataset_name, "");
    const run = str(asset.run_name ?? metadata.run_name, "");
    const originType = str(asset.origin_type ?? metadata.origin_type, "ASSET");
    const secondary = [model, checkpoint && `Checkpoint: ${checkpoint}`, run && `Run: ${run}`, baseModel, dataset, step !== undefined && step !== null ? `Step ${step}` : "", seed !== undefined && seed !== null ? `Seed ${seed}` : ""].filter(Boolean).join(" · ") || str(asset.kind, "Image");
    return <a className="asset-card" key={idOf(asset) || index} href={hrefForAsset ? hrefForAsset(asset) : galleryOpenHref(asset)} title={`Open ${name}`}>
      <span className={`asset-origin-badge origin-${originType.toLowerCase()}`}>{originType}</span><AssetImage assetRevisionId={String(asset.asset_revision_id ?? asset.asset_id ?? asset.id)} alt={name} loading="lazy" />
      <span>{name}</span><small>{secondary}</small>
    </a>;
  })}</div>;
}
