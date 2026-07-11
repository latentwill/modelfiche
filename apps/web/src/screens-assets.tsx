import { Fragment, useEffect, useState, type CSSProperties } from "react";
import {
  Archive,
  Check,
  Database,
  Download,
  ExternalLink,
  FileImage,
  FolderInput,
  Grid3X3,
  Plus,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, patchBody, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Field, Form, Json, Notice, Page, Panel, SelectRecords, Status } from "./ui";

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
type LossMetricPoint = { step: number; value: number; wall_time: number | null };
type LossMetrics = {
  run_id?: string;
  metric_name?: string;
  points?: unknown;
  final_value?: number | null;
  source_key?: string | null;
};

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

  const width = 560;
  const height = 180;
  const padding = 24;
  const minStep = normalized[0].step;
  const maxStep = normalized[normalized.length - 1].step;
  const minValue = Math.min(...normalized.map(point => point.value));
  const maxValue = Math.max(...normalized.map(point => point.value));
  const stepRange = maxStep - minStep || 1;
  const valueRange = maxValue - minValue || 1;
  const coordinate = (value: number) => Number(value.toFixed(2)).toString();
  const x = (step: number) => padding + ((step - minStep) / stepRange) * (width - padding * 2);
  const y = (value: number) => height - padding - ((value - minValue) / valueRange) * (height - padding * 2);
  const path = normalized.map((point, index) => `${index ? "L" : "M"} ${coordinate(x(point.step))} ${coordinate(y(point.value))}`).join(" ");

  return <div className="loss-graph" role="img" aria-label="Loss over training step">
    <svg className="loss-graph-svg" viewBox={`0 0 ${width} ${height}`} aria-hidden="true" focusable="false">
      <line data-loss-axis="y" x1={padding} y1={padding} x2={padding} y2={height - padding} />
      <line data-loss-axis="x" x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} />
      <path data-loss-path d={path} />
      <text x={padding} y={height - 7}>Step {minStep}</text>
      <text x={width - padding} y={height - 7} textAnchor="end">Step {maxStep}</text>
      <text x={padding} y={padding - 8}>Loss {maxValue.toPrecision(3)}</text>
      <text x={padding} y={height - padding - 7} textAnchor="start">Loss {minValue.toPrecision(3)}</text>
    </svg>
  </div>;
}


function ActionMessage({ message, error }: { message?: string; error?: string }) {
  if (error) return <div className="vela-notice vela-notice-error" role="alert"><strong>Action failed</strong><span>{error}</span></div>;
  if (message) return <div className="vela-notice" role="status" aria-live="polite"><strong>Updated</strong><span>{message}</span></div>;
  return null;
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
  const data = resource.data;
  const versions = rows(data?.versions).length ? rows(data?.versions) : rows(versionsResource.data);
  const currentVersionId = str(data?.current_version_id ?? data?.version_id ?? versions[0]?.id, "");
  const activeVersionId = selectedVersionId || currentVersionId;
  const activeVersion = versions.find(version => idOf(version) === activeVersionId);
  const publishedItems = useResource<unknown>(activeVersionId ? `/api/dataset-versions/${activeVersionId}/items` : null);
  const items = draft ? rows(draft.items) : rows(publishedItems.data);

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

    <Panel title="Caption versions">
      <Notice loading={versionsResource.loading && !versions.length} empty={!versionsResource.loading && !versions.length} />
      {!!versions.length && <div className="version-tracker" aria-label="Dataset versions">
        {versions.map(version => {
          const versionId = idOf(version);
          const isActive = !draft && versionId === activeVersionId;
          return <button
            key={versionId}
            className="vela-project-card"
            aria-pressed={isActive}
            disabled={Boolean(draft)}
            title={draft ? "Publish or discard the working draft before changing the base version" : `View ${str(version.name, `version ${version.version_number}`)}`}
            onClick={() => { setSelectedVersionId(versionId); setSelected([]); setPreview(null); }}
          >
            <span>v{str(version.version_number)}</span>
            <strong>{str(version.name, "Imported")}</strong>
            <small>{versionId === currentVersionId ? "current" : "published"} · {str(version.item_count, "0")} items</small>
          </button>;
        })}
        {draft && <button className="draft vela-project-card" aria-pressed="true" disabled title="The working copy is the active caption version"><span>Draft</span><strong>Working copy</strong><small>unpublished</small></button>}
      </div>}
    </Panel>

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
      {!!items.length && <div className="caption-list">{items.map((item, index) => <CaptionItem key={idOf(item) || index} item={item} selected={selected.includes(idOf(item))} onSelect={checked => setSelected(checked ? [...selected, idOf(item)] : selected.filter(value => value !== idOf(item)))} draftId={str(draft?.id, "")} onSaved={created => {
        if (!draft) return;
        const next = [...items]; next[index] = { ...item, ...created }; setDraft({ ...draft, items: next });
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
      <a href={`#/image/${String(assetId)}`} title={`Open ${filename} in the image reviewer`}><AssetImage assetRevisionId={String(assetId)} alt={filename} loading="lazy" /><span>Open image</span></a>
    </div>
    <div>
      <div className="caption-heading"><strong>{filename}</strong><Status value={item.caption_format ?? "text"} /></div>
      <textarea aria-label={`Caption for ${filename}`} value={caption} readOnly={!draftId} title={!draftId ? "Create a new version to edit this caption" : undefined} onChange={event => setCaption(event.target.value)} />
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
      await Promise.all([run.reload(), samples.reload(), checkpointsResource.reload(), metrics.reload()]);
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
    {run.data && <div className="run-summary vela-context-stats">
      <div className="vela-stat"><span>Status</span><Status value={run.data.status} /></div>
      <div className="vela-stat"><span>LoRA rank</span><strong>{str(run.data.lora_rank ?? run.data.network_dim, "-")}</strong></div>
      <div className="vela-stat"><span>Checkpoints</span><strong>{checkpoints.length}</strong></div>
      <div className="vela-stat"><span>Samples</span><strong>{sampleRows.length}</strong></div>
    </div>}

    <Panel title="Training metrics">
      <Notice error={metrics.error} loading={metrics.loading} />
      <div className="metrics-grid"><div><strong>{str(metrics.data?.final_value, "Unavailable")}</strong><span>Final loss</span></div><div><strong>{str(run.data?.learning_rate, "Unavailable")}</strong><span>Learning rate</span></div><LossGraph points={metrics.data?.points} /></div>
    </Panel>

    <Panel title="Checkpoint timeline">
      <Notice error={!enrichedCheckpoints.length ? checkpointsResource.error : undefined} loading={checkpointsResource.loading && !enrichedCheckpoints.length} empty={!checkpointsResource.loading && !checkpoints.length} />
      {!!checkpoints.length && <div className="checkpoint-timeline">{checkpoints.map(checkpoint => <button className={selectedCheckpointId === idOf(checkpoint) ? "active" : ""} aria-pressed={selectedCheckpointId === idOf(checkpoint)} key={idOf(checkpoint)} onClick={() => setSelectedCheckpointId(idOf(checkpoint))}><strong>Step {str(checkpoint.step)}</strong><Status value={checkpoint.hydration_state ?? checkpoint.state} /><span>{str(checkpoint.filename ?? checkpoint.name)}</span></button>)}</div>}
      {selectedCheckpoint && <div className="checkpoint-detail"><dl className="details"><dt>File</dt><dd>{str(selectedCheckpoint.filename ?? selectedCheckpoint.name)}</dd><dt>Size</dt><dd>{formatBytes(selectedCheckpoint.size)}</dd><dt>Storage</dt><dd>{str(selectedCheckpoint.hydration_state, "remote")}</dd><dt>Model version</dt><dd>{str(selectedCheckpoint.model_version_name, "Not registered")}</dd></dl><div className="actions"><a className="button vela-button" href={`#/checkpoint/${idOf(selectedCheckpoint)}`}><ExternalLink size={14} />Open checkpoint</a><button className="primary" onClick={() => void hydrateCheckpoint(selectedCheckpoint)} disabled={busy === `hydrate:${idOf(selectedCheckpoint)}`}><Download size={14} />{busy === `hydrate:${idOf(selectedCheckpoint)}` ? "Queuing..." : "Hydrate"}</button></div></div>}
    </Panel>

    <Panel title="Samples by checkpoint">
      <Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />
      {!!sampleSteps.length && <div className="sample-step-strip">{sampleSteps.map(step => <a href={`#/samples/${id}?step=${encodeURIComponent(step)}`} key={step}><strong>Step {step}</strong><span>{sampleRows.filter(sample => str(sample.step, "unassigned") === step).length} images</span></a>)}</div>}
      {!!sampleRows.length && <AssetGrid assets={sampleRows.slice(0, 12)} />}
    </Panel>

    <div className="dashboard-lower vela-dashboard-grid">
      <Panel title="Normalized configuration"><Notice error={config.error} loading={config.loading} />{config.data && <ConfigDetails value={(config.data.normalized ?? config.data.normalized_config ?? config.data) as Row} />}</Panel>
      <Panel title="Raw AI Toolkit config"><Notice error={config.error} loading={config.loading} />{!config.loading && <Json value={config.data?.raw ?? config.data?.raw_config ?? run.data?.raw_config ?? { state: "Not imported" }} />}</Panel>
    </div>
    <Panel title="Lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} />{!!rows(lineage.data).length && <DataTable rows={rows(lineage.data)} columns={[["source_type", "From"], ["relationship", "Relationship"], ["target_type", "To"], ["created_at", "Recorded"]]} />}</Panel>
  </Page>;
}

function ConfigDetails({ value }: { value: Row }) {
  return <dl className="details">{Object.entries(value).slice(0, 14).map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof item === "object" ? JSON.stringify(item) : str(item)}</dd></Fragment>)}</dl>;
}

export function CheckpointScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/checkpoints/${id}`);
  const models = useResource<unknown>("/api/models");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [hydrating, setHydrating] = useState(false);
  const checkpointAsset = resource.data?.asset && typeof resource.data.asset === "object" ? resource.data.asset as Row : {};
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
    {resource.data && <Panel title="Checkpoint record"><Json value={resource.data} /></Panel>}
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
  const [selectedVersion, setSelectedVersion] = useState<Row | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const selectedState = str(selectedVersion?.lifecycle_state, "").toLowerCase();
  const latestVersion = versions[0] ?? {};
  const importedRunId = str(resource.data?.description).match(/training run ([0-9a-f-]{36})/i)?.[1] ?? "";
  const versionRunIds = new Set(versions.map(version => str((version.readiness as Row | undefined)?.training_run_id)).filter(Boolean));
  if (importedRunId) versionRunIds.add(importedRunId);
  const projectRuns = rows(runs.data);
  const relatedRuns = projectRuns.filter(run => versionRunIds.has(idOf(run)) || (!versionRunIds.size && str(run.name) === str(resource.data?.name)));

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
    {projectId ? <><a className="button vela-button vela-button-primary" href={`#/evals?project=${projectId}`}><Sparkles size={15} />New eval</a><a className="button vela-button" href={`#/grids?project=${projectId}`}><Grid3X3 size={15} />New grid</a></> : <button disabled title="The model is not attached to a project">Evaluation unavailable</button>}
    {idOf(latestVersion) ? <a className="button vela-button" href={`#/transfers?mode=export&model_version=${idOf(latestVersion)}`}><Download size={15} />Export version</a> : <button disabled title="Register a model version before exporting"><Download size={15} />Export unavailable</button>}
  </>}>
    <Notice error={resource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    {resource.data && <Panel title="Model record"><Notice error={project.error} loading={project.loading} /><dl className="details"><dt>Description</dt><dd>{str(resource.data.description, "No description")}</dd><dt>Latest trigger words</dt><dd>{Array.isArray(latestVersion.trigger_words) && latestVersion.trigger_words.length ? latestVersion.trigger_words.join(", ") : "None registered"}</dd><dt>Base model</dt><dd>{str(latestVersion.base_model, "Not registered")}</dd><dt>Project</dt><dd>{projectId ? <a href={`#/project/${projectId}`}>{str(project.data?.title ?? project.data?.name, "Open project")}</a> : "Not assigned"}</dd><dt>Versions</dt><dd>{versions.length}</dd></dl></Panel>}

    <Panel title="Project eval runs">
      <Notice error={evals.error} loading={evals.loading} empty={!evals.loading && !rows(evals.data).length} />
      {!!rows(evals.data).length && <div className="eval-run-strip">{rows(evals.data).map(evaluation => <a href={`#/eval/${idOf(evaluation)}`} key={idOf(evaluation)}><div className="eval-thumbs">{rows(evaluation.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={String(output.asset_revision_id ?? output.asset_id)} alt="Eval output" />)}</div><strong>{str(evaluation.name, "Eval run")}</strong><span>{str(evaluation.prompt_set_name)} · {str(evaluation.output_count, "0")} outputs</span><Status value={evaluation.status} /></a>)}</div>}
    </Panel>

    <Panel title="Model versions">
      <Notice loading={resource.loading} empty={!resource.loading && !versions.length} />
      {!!versions.length && <DataTable rows={versions} columns={[["name", "File"], ["checkpoint_step", "Step"], ["lifecycle_state", "State"], ["fal_status", "FAL"], ["created_at", "Created"]]} onRow={setSelectedVersion} />}
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
  const sampleRows = rows(samples.data);
  return <details className="training-run" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><strong>{str(run.name, "Training run")}</strong><span>{str(run.dataset_version_name, "No linked dataset")}</span><Status value={run.status} /></summary>
    <div className="training-run-body">
      <dl className="details"><dt>Base</dt><dd>{str(run.base_model)}</dd><dt>Checkpoints</dt><dd>{str(run.checkpoint_count, "0")}</dd><dt>Samples</dt><dd>{str(run.sample_count, "0")}</dd><dt>Dataset</dt><dd>{str(run.dataset_version_name, "Not linked")}</dd></dl>
      <div className="actions"><a className="button vela-button" href={`#/run/${runId}`}><ExternalLink size={14} />Open run and loss graph</a><a className="button vela-button" href={`#/samples/${runId}`}><FileImage size={14} />View all samples</a></div>
      {expanded && <>
        <section className="vela-section"><div className="vela-section-heading"><h3>Loss graph</h3></div><Notice error={metrics.error} loading={metrics.loading} /><LossGraph points={metrics.data?.points} /></section>
        <section className="vela-section"><div className="vela-section-heading"><h3>Sample images</h3></div><Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />{!!sampleRows.length && <AssetGrid assets={sampleRows.slice(0, 8)} />}</section>
      </>}
    </div>
  </details>;
}

export function GalleryScreen({ projectId = "" }: { projectId?: string }) {
  const projects = useResource<unknown>("/api/projects?limit=100");
  const defaultFilters = { project_id: projectId, kind: "image", decision: "", rating: "", q: "", include_dataset_assets: false, limit: 2000 };
  const [filters, setFilters] = useState(defaultFilters);
  const [size, setSize] = useState(170);
  const path = `/api/gallery${query(filters)}`;
  const resource = useResource<unknown>(path);
  const assets = rows(resource.data);
  const isFiltered = Object.entries(filters).some(([key, value]) => value !== defaultFilters[key as keyof typeof defaultFilters]);
  return <Page title="Gallery" subtitle="Training samples, evals, and generated assets; dataset members are opt-in" actions={<a className="button vela-button vela-button-primary" href="#/transfers?mode=import"><Upload size={15} />Import assets</a>}>
    <Panel title="Filters">
      <Notice error={projects.error} loading={projects.loading} />
      <div className="form-grid">
        <Field label="Project"><select value={filters.project_id} onChange={event => setFilters({ ...filters, project_id: event.target.value })}><option value="">All projects</option>{rows(projects.data).map(project => <option key={idOf(project)} value={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>
        <Field label="Search"><input value={filters.q} onChange={event => setFilters({ ...filters, q: event.target.value })} placeholder="Filename" /></Field>
        <Field label="Decision"><select value={filters.decision} onChange={event => setFilters({ ...filters, decision: event.target.value })}><option value="">Any</option><option value="candidate">Candidate</option><option value="approved">Approved</option><option value="hold">Hold</option><option value="reject">Reject</option></select></Field>
        <Field label="Rating"><select value={filters.rating} onChange={event => setFilters({ ...filters, rating: event.target.value })}><option value="">Any</option>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value} star{value === 1 ? "" : "s"}</option>)}</select></Field>
        <Field label="Dataset images"><label><input type="checkbox" checked={filters.include_dataset_assets} onChange={event => setFilters({ ...filters, include_dataset_assets: event.target.checked })} /> Include dataset members</label></Field>
        <Field label={`Thumbnail size · ${size}px`}><input aria-label="Thumbnail size" type="range" min="120" max="260" value={size} onChange={event => setSize(Number(event.target.value))} /></Field>
      </div>
      <div className="actions"><button onClick={() => setFilters(defaultFilters)} disabled={!isFiltered} title={!isFiltered ? "No additional filters are active" : undefined}><X size={14} />Clear filters</button><span className="vela-meta" aria-live="polite">{assets.length} matching assets</span></div>
    </Panel>
    <Panel title="Assets">
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !assets.length} />
      {!!assets.length && <div style={{ "--asset-size": `${size}px` } as CSSProperties}><AssetGrid assets={assets} /></div>}
    </Panel>
  </Page>;
}

export function AssetGrid({ assets }: { assets: Row[] }) {
  return <div className="asset-grid">{assets.map((asset, index) => {
    const name = str(asset.filename ?? asset.name, "Generated image");
    const metadata = asset.metadata && typeof asset.metadata === "object" ? asset.metadata as Row : {};
    const step = asset.step ?? metadata.step;
    const seed = asset.seed ?? metadata.seed;
    const secondary = [step !== undefined && step !== null ? `Step ${step}` : "", seed !== undefined && seed !== null ? `Seed ${seed}` : ""].filter(Boolean).join(" · ") || str(asset.kind, "Image");
    return <a className="asset-card" key={idOf(asset) || index} href={`#/image/${String(asset.asset_revision_id ?? asset.asset_id ?? asset.id)}`} title={`Open ${name}`}>
      <AssetImage assetRevisionId={String(asset.asset_revision_id ?? asset.asset_id ?? asset.id)} alt={name} loading="lazy" />
      <span>{name}</span><small>{secondary}</small>
    </a>;
  })}</div>;
}
