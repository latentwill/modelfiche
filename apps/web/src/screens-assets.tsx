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
  Settings2,
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
import { useResource, useRunLive } from "./hooks";
import { DataTable, Dropdown, Field, Form, Json, Notice, Page, Pagination, Panel, SelectRecords, Status, useFocusWorkspace } from "./ui";
import { generationReplayFromMetadata, generatorHref, loraScaleFromMetadata } from "./generation";
import { DitherLossChart } from "./dither-loss-chart";
import { LineageList } from "./lineage";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const DEFAULT_VISIBLE_SCENE_CAPTION_PROMPT = "Caption each image in one concise sentence describing only what is visibly present in the scene: subjects, objects, actions, setting, and spatial relationships. Do not describe style, lighting, mood, quality, or camera characteristics.";
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
export const scientificNotation = (value: unknown) => {
  const scalar = value && typeof value === "object" && !Array.isArray(value)
    ? (value as Row).value_text ?? (value as Row).value
    : value;
  const number = Number(scalar);
  if (!Number.isFinite(number)) return str(scalar, "Unavailable");
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

  return <DitherLossChart points={normalized} />;
}

function FlintMetricChart({ points, label }: { points: LossMetricPoint[]; label: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const context = canvas.current?.getContext?.("2d");
    if (!context || !points.length || !canvas.current) return;
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
    const styles = getComputedStyle(canvas.current);
    const color = styles.getPropertyValue("--vela-accent").trim() || styles.color;
    const backgroundColor = styles.getPropertyValue("--vela-accent-soft").trim() || "transparent";
    const dataset = assembled.data?.datasets?.[0];
    if (dataset) Object.assign(dataset, { label, borderColor: color, backgroundColor, pointRadius: points.length > 300 ? 0 : 1.5, borderWidth: 2, tension: .18, fill: true });
    assembled.options = { ...assembled.options, responsive: true, maintainAspectRatio: false, animation: false };
    const chart = new Chart(context, assembled);
    return () => chart.destroy();
  }, [label, points]);
  return <div className="flint-metric-chart" role="img" aria-label={`${label} over training step`}><canvas ref={canvas} /></div>;
}


function ActionMessage({ message, error }: { message?: string; error?: string }) {
  if (error) return <div className="vela-notice vela-notice-error" role="alert"><strong>Action failed</strong><span>{error}</span></div>;
  if (message) return <div className="vela-notice" role="status" aria-live="polite"><strong>Updated</strong><span>{message}</span></div>;
  return null;
}

type BatchScope = "visible" | "selected" | "all";
type CaptionOperationAction = "replace" | "add_word" | "remove_word" | "set_included";
type CaptionOperationPreview = Row & {
  action: CaptionOperationAction;
  included?: boolean;
  preview_token: string;
  target_item_ids: string[];
};

function parsedStructuredCaption(value: unknown) {
  const caption = str(value, "");
  if (!caption.trim()) return null;
  try {
    const parsed = JSON.parse(caption);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function structuredCaptionSummary(value: unknown, fallback: string) {
  const parsed = parsedStructuredCaption(value);
  if (!parsed) return str(value, fallback);
  const record = !Array.isArray(parsed) ? parsed as Row : {};
  const preferred = ["description", "caption", "summary", "text", "scene", "subject"];
  const summary = preferred.map(key => record[key]).find(item => typeof item === "string" && item.trim());
  if (typeof summary === "string") return summary.trim().slice(0, 240);
  return fallback;
}

function captionPresentation(item: Row, fallback: string) {
  const raw = str(item.caption ?? item.prompt, "");
  const structured = parsedStructuredCaption(raw);
  const format = str(item.caption_format, "text") === "json" || structured ? "json" : "text";
  return {
    format,
    raw,
    rendered: structured ? JSON.stringify(structured, null, 2) : raw,
    alt: structuredCaptionSummary(raw, fallback),
  };
}

function ObjectImageStrip({ assets, label, showTrainingDetails = false, datasetId = "", projectId = "" }: { assets: Row[]; label: string; showTrainingDetails?: boolean; datasetId?: string; projectId?: string }) {
  if (!assets.length) return null;
  return <div className="object-image-strip" aria-label={label}>{assets.slice(0, 4).map(asset => {
    const caption = captionPresentation(asset, str(asset.name, label));
    const step = str(asset.training_step ?? asset.step, "");
    const uploadState = str(asset.upload_status ?? asset.upload_state ?? asset.availability, "");
    return <a href={galleryOpenHref({ ...asset, dataset_id: datasetId || asset.dataset_id, project_id: projectId || asset.project_id })} key={idOf(asset)}>
      <AssetImage assetRevisionId={str(asset.asset_revision_id ?? asset.asset_id ?? asset.id)} alt={caption.alt} loading="lazy" />
      {showTrainingDetails && (caption.raw || step || uploadState) && <span className="training-sample-details">
        {caption.raw && <strong>{caption.alt}</strong>}
        <small>{[step && `Step ${step}`, uploadState && `Upload ${uploadState}`].filter(Boolean).join(" · ")}</small>
      </span>}
    </a>;
  })}</div>;
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
  const captionSettings = useResource<Row>("/api/operator-settings");
  const activeDraftResource = useResource<Row>(id ? `/api/datasets/${id}/drafts/active` : null);
  const [draft, setDraft] = useState<Row | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [viewingDraft, setViewingDraft] = useState(false);
  const [find, setFind] = useState("");
  const [replace, setReplace] = useState("");
  const [word, setWord] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [captionMethod, setCaptionMethod] = useState<"manual" | "ai">("manual");
  const [captionPrompt, setCaptionPrompt] = useState(DEFAULT_VISIBLE_SCENE_CAPTION_PROMPT);
  const [captionFormat, setCaptionFormat] = useState<"text" | "json">("text");
  const [captionModel, setCaptionModel] = useState("");
  const [captionModelFilter, setCaptionModelFilter] = useState("");
  const [captionPreset, setCaptionPreset] = useState("");
  const [captionPresetName, setCaptionPresetName] = useState("Visible scene");
  const [batchScope, setBatchScope] = useState<BatchScope>("visible");
  const [captionBusy, setCaptionBusy] = useState(false);
  const [captionProgress, setCaptionProgress] = useState<{ completed: number; total: number; filename: string } | null>(null);
  const [captionPresetBusy, setCaptionPresetBusy] = useState(false);
  const [preview, setPreview] = useState<CaptionOperationPreview | null>(null);
  const [captionGenerationReview, setCaptionGenerationReview] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const data = resource.data;
  const activeDraft = viewingDraft ? draft : null;
  const operationHistory = useResource<unknown>(activeDraft?.id ? `/api/dataset-drafts/${activeDraft.id}/operations` : null);
  const captioning = (captionSettings.data?.captioning ?? {}) as Row;
  const captionPresets = rows(captioning.presets);
  const captionLlm = (captionSettings.data?.llm ?? {}) as Row;
  const captionProvider = str(captionLlm.provider, "openai");
  const configuredCaptionModel = str(captionLlm.model, "");
  const captionCatalog = useResource<Row>(
    captionMethod === "ai"
      ? `/api/operator-settings/llm-models${query({
          provider: captionProvider,
          base_url: str(captionLlm.base_url, "") || undefined,
          capability: "image",
        })}`
      : null,
  );
  const captionCatalogModels = rows(captionCatalog.data);
  const captionModelOptions = captionCatalogModels.map(model => ({
    value: str(model.id),
    label: str(model.label, str(model.id)),
    description: [str(model.id) !== str(model.label) ? str(model.id) : "", str(model.description)].filter(Boolean).join(" · "),
  }));
  if (captionModel && !captionModelOptions.some(option => option.value === captionModel)) {
    captionModelOptions.unshift({
      value: captionModel,
      label: captionModel,
      description: captionCatalog.loading ? "Loading provider catalog…" : "Configured model (modality not published by provider)",
    });
  }
  const normalizedCaptionModelFilter = captionModelFilter.trim().toLowerCase();
  const visibleCaptionModelOptions = normalizedCaptionModelFilter
    ? captionModelOptions.filter(option => option.value === captionModel || `${option.label} ${option.value} ${option.description}`.toLowerCase().includes(normalizedCaptionModelFilter))
    : captionModelOptions;
  const versions = rows(data?.versions).length ? rows(data?.versions) : rows(versionsResource.data);
  const currentVersionId = str(data?.current_version_id ?? data?.version_id ?? versions[0]?.id, "");
  const activeVersionId = selectedVersionId || currentVersionId;
  const activeVersion = versions.find(version => idOf(version) === activeVersionId);
  const activeVersionReferenced = activeVersion?.status === "referenced";
  const publishedItems = useResource<Row>(activeVersionId && !activeDraft ? `/api/dataset-versions/${activeVersionId}/items${query({ limit: pageSize, offset: (page - 1) * pageSize, paginated: true })}` : null);
  const items = activeDraft ? rows(activeDraft.items) : rows(publishedItems.data?.items);
  const totalItems = activeDraft ? items.length : Number(publishedItems.data?.total ?? activeVersion?.item_count ?? 0);
  const visibleItems = activeDraft ? items.slice((page - 1) * pageSize, page * pageSize) : items;
  const visibleItemIds = visibleItems.map(item => idOf(item)).filter(Boolean);
  const batchTargetIds = batchScope === "all" ? [] : batchScope === "visible" ? visibleItemIds : selected;
  const batchTargetCount = batchScope === "all" ? items.length : batchTargetIds.length;
  const firstVisible = visibleItems.length ? (page - 1) * pageSize + 1 : 0;
  const lastVisible = Math.min(page * pageSize, totalItems);
  const draftFingerprint = items.map(item => [
    idOf(item),
    str(item.caption),
    str(item.caption_format),
    String(Boolean(item.included)),
    JSON.stringify(item.tags ?? []),
  ].join("\u001f")).join("\u001e");
  const captionGenerationFingerprint = JSON.stringify({
    draft_id: activeDraft?.id,
    draft: draftFingerprint,
    scope: batchScope,
    item_ids: batchScope === "all" ? items.map(item => idOf(item)) : batchTargetIds,
    prompt: captionPrompt.trim(),
    provider: captionProvider,
    model: captionModel.trim(),
    caption_format: captionFormat,
  });

  useEffect(() => setPage(1), [activeVersionId, pageSize]);
  useEffect(() => {
    if (!activeDraft) setCaptionFormat(str(activeVersion?.caption_format, "text") === "json" ? "json" : "text");
  }, [activeVersion?.caption_format, activeDraft]);
  useEffect(() => {
    if (!captionModel && configuredCaptionModel) setCaptionModel(configuredCaptionModel);
  }, [captionModel, configuredCaptionModel]);
  useEffect(() => {
    if (draft || !activeDraftResource.data) return;
    setDraft(activeDraftResource.data);
    setSelectedVersionId(str(activeDraftResource.data.base_version_id));
    setViewingDraft(true);
    setMessage("Restored the active working draft.");
  }, [activeDraftResource.data, draft]);
  useEffect(() => {
    setPreview(null);
  }, [activeDraft?.id, draftFingerprint, batchScope, selected.join("\u001f"), find, replace, word, captionFormat, page, pageSize, activeVersionId]);
  useEffect(() => {
    setCaptionGenerationReview("");
  }, [captionGenerationFingerprint]);

  if (resource.error && !data) {
    return <Page title="Dataset unavailable" subtitle="The requested dataset could not be loaded."><Notice error={resource.error} /></Page>;
  }
  if (resource.loading && !data) {
    return <Page title="Loading dataset" subtitle="Loading versions, items, and caption state."><Notice loading /></Page>;
  }


  async function createDraft() {
    if (!activeVersionId) return;
    setBusy("draft"); setActionError(""); setMessage("");
    try {
      const created = await api<Row>(`/api/datasets/${id}/drafts`, jsonBody({ base_version_id: activeVersionId }));
      const loaded = await api<Row>(`/api/dataset-drafts/${created.id}`);
      setDraft(loaded); setViewingDraft(true); activeDraftResource.setData(loaded);
      setMessage(`Working draft created from ${str(activeVersion?.name, "the selected version")}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  function operationPayload(action: CaptionOperationAction, included?: boolean, previewToken?: string) {
    const parameters = action === "replace" ? { find, replace } : action === "set_included" ? { included: Boolean(included) } : { word };
    return {
      operation: action,
      parameters,
      item_ids: batchScope === "all" ? null : batchTargetIds,
      all: batchScope === "all",
      preview_token: previewToken,
    };
  }

  async function previewOperation(action: CaptionOperationAction, included?: boolean) {
    if (!draft?.id || !batchTargetCount) return;
    setBusy(`preview-${action}`); setActionError(""); setMessage("");
    try {
      const result = await api<Row>(`/api/dataset-drafts/${draft.id}/operations/preview`, jsonBody(operationPayload(action, included)));
      const targetIds = Array.isArray(result.target_item_ids) ? result.target_item_ids.map(value => String(value)) : [];
      setPreview({ ...result, action, included, preview_token: str(result.preview_token), target_item_ids: targetIds });
    } catch (reason) {
      setPreview(null);
      setActionError(errorText(reason));
    } finally {
      setBusy("");
    }
  }

  async function applyPreview() {
    if (!draft?.id || !preview?.preview_token) return;
    const { action, included, preview_token: previewToken } = preview;
    setBusy(`apply-${action}`); setActionError(""); setMessage("");
    try {
      await api(`/api/dataset-drafts/${draft.id}/operations`, jsonBody(operationPayload(action, included, previewToken)));
      const loaded = await api<Row>(`/api/dataset-drafts/${draft.id}`);
      setDraft(loaded);
      setPreview(null);
      await operationHistory.reload();
      setMessage(action === "add_word" ? "Previewed word addition applied." : action === "remove_word" ? "Previewed word removal applied." : action === "set_included" ? `Previewed images ${included ? "included" : "excluded"}.` : "Previewed replacement applied.");
    } catch (reason) {
      setPreview(null);
      setActionError(errorText(reason));
    } finally {
      setBusy("");
    }
  }

  async function undoOperation(operationId: string) {
    if (!draft?.id) return;
    setBusy(`undo-${operationId}`); setActionError(""); setMessage("");
    try {
      await api(`/api/dataset-drafts/${draft.id}/operations/${operationId}/undo`, { method: "POST" });
      setDraft(await api<Row>(`/api/dataset-drafts/${draft.id}`));
      await operationHistory.reload();
      setMessage("Caption operation undone.");
    } catch (reason) {
      setActionError(errorText(reason));
    } finally {
      setBusy("");
    }
  }
  async function generateCaptions() {
    if (!draft?.id || captionMethod !== "ai" || !captionPrompt.trim() || !captionModel.trim() || !batchTargetCount || captionGenerationReview !== captionGenerationFingerprint) return;
    const targetIds = new Set(batchTargetIds);
    const targetItems = items.filter(item => {
      const itemId = idOf(item);
      return itemId && (batchScope === "all" || targetIds.has(itemId));
    });
    if (!targetItems.length) return;
    setCaptionGenerationReview("");
    let completed = 0;
    let changed = 0;
    setCaptionBusy(true); setActionError(""); setMessage("");
    setCaptionProgress({ completed, total: targetItems.length, filename: str(targetItems[0].filename, "image") });
    try {
      for (const target of targetItems) {
        const itemId = idOf(target);
        setCaptionProgress({ completed, total: targetItems.length, filename: str(target.filename, `image ${completed + 1}`) });
        const result = await api<Row>(`/api/dataset-drafts/${draft.id}/caption`, jsonBody({
          prompt: captionPrompt.trim(),
          model: captionModel.trim(),
          caption_format: captionFormat,
          item_ids: [itemId],
          all: false,
        }));
        const generated = rows(result.items);
        changed += Number(result.changed ?? generated.length);
        if (generated.length) setDraft(previous => {
          if (!previous) return previous;
          const generatedById = new Map(generated.map(item => [idOf(item), item]));
          return { ...previous, items: rows(previous.items).map(item => generatedById.get(idOf(item)) ? { ...item, ...generatedById.get(idOf(item)) } : item) };
        });
        completed += 1;
        setCaptionProgress({ completed, total: targetItems.length, filename: str(target.filename, `image ${completed}`) });
      }
      await operationHistory.reload();
      setMessage(`Generated ${changed} captions across ${completed} ${batchScope === "all" ? "draft" : batchScope} images. Changes remain in the working draft until you publish.`);
    } catch (reason) {
      setActionError(`Caption generation stopped after ${completed} of ${targetItems.length} images: ${errorText(reason)}`);
    } finally {
      setCaptionBusy(false);
      setCaptionProgress(null);
    }
  }

  async function updateCaptionFormat(value: "text" | "json") {
    if (!draft?.id) return;
    const previous = captionFormat;
    setCaptionFormat(value); setCaptionBusy(true); setActionError(""); setMessage("");
    try {
      const result = await api<Row>(`/api/dataset-drafts/${draft.id}/caption-format`, patchBody({ caption_format: value }));
      setDraft(current => current ? { ...current, items: rows(result.items) } : current);
      await operationHistory.reload();
      setMessage(`Caption format set to ${value.toUpperCase()} for the working draft.`);
    } catch (reason) { setCaptionFormat(previous); setActionError(errorText(reason)); } finally { setCaptionBusy(false); }
  }

  async function saveCaptionPreset() {
    if (!captionPresetName.trim() || !captionPrompt.trim()) return;
    setCaptionPresetBusy(true); setActionError(""); setMessage("");
    try {
      const presets = captionPresets
        .filter(preset => str(preset.name).localeCompare(captionPresetName.trim(), undefined, { sensitivity: "accent" }) !== 0)
        .map(preset => ({ name: str(preset.name), prompt: str(preset.prompt) }));
      const updated = await api<Row>("/api/operator-settings", patchBody({ captioning: { presets: [...presets, { name: captionPresetName.trim(), prompt: captionPrompt.trim() }] } }));
      captionSettings.setData(updated); setCaptionPreset(captionPresetName.trim()); setMessage(`Caption preset "${captionPresetName.trim()}" saved.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setCaptionPresetBusy(false); }
  }

  async function deleteCaptionPreset() {
    if (!captionPreset) return;
    setCaptionPresetBusy(true); setActionError(""); setMessage("");
    try {
      const updated = await api<Row>("/api/operator-settings", patchBody({ captioning: { presets: captionPresets.filter(preset => str(preset.name) !== captionPreset).map(preset => ({ name: str(preset.name), prompt: str(preset.prompt) })) } }));
      captionSettings.setData(updated); setCaptionPreset(""); setMessage(`Caption preset "${captionPreset}" removed.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setCaptionPresetBusy(false); }
  }

  async function discardDraft() {
    if (!draft?.id || !window.confirm("Discard this working draft? Published versions will not be changed.")) return;
    setBusy("discard"); setActionError("");
    try {
      await api(`/api/dataset-drafts/${draft.id}`, { method: "DELETE" });
      activeDraftResource.setData(null); setDraft(null); setViewingDraft(false); setSelected([]); setPreview(null); setBatchScope("visible"); setMessage("Working draft discarded.");
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  const subtitle = `${str(data?.project_name, "Project dataset")} · ${totalItems} images · ${activeDraft ? "Working draft" : `v${str(activeVersion?.version_number ?? data?.current_version, "-")}`}`;
  return <Page title={str(data?.name, "Dataset")} subtitle={subtitle} actions={<>
    {activeVersionId && !activeDraft ? <a className="button vela-button" href={`#/transfers?mode=export&dataset_version=${activeVersionId}`}><Download size={15} />Export version</a> : <button disabled title="Select a published dataset version before exporting"><Download size={15} />Export unavailable</button>}
    {!activeDraft && activeVersion?.status === "published" && <a className="button vela-button vela-button-primary" href={`#/training-new?dataset=${id}&version=${activeVersionId}`}><Sparkles size={15} />Start training</a>}
    {!activeDraft && !draft && <button className="vela-button" onClick={() => void createDraft()} disabled={!activeVersionId || busy === "draft"} title={!activeVersionId ? "Import a dataset version before editing captions" : undefined}><Plus size={15} />{busy === "draft" ? "Creating..." : "Clone as new version"}</button>}
    {!activeDraft && draft && <button className="vela-button" onClick={() => { setSelectedVersionId(str(draft.base_version_id)); setViewingDraft(true); setSelected([]); setPreview(null); }}><Plus size={15} />Return to working draft</button>}
  </>}>
    <Notice error={resource.error || versionsResource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    <ObjectImageStrip assets={items} label="Dataset image preview" datasetId={id} projectId={str(data?.project_id)} />

    <Panel title="Caption versions">
      <Notice loading={versionsResource.loading && !versions.length} empty={!versionsResource.loading && !versions.length} />
      {!!versions.length && <div className="caption-versions" aria-label="Dataset caption versions">
        {versions.map(version => {
          const versionId = idOf(version);
          const isActive = !activeDraft && versionId === activeVersionId;
          const isCurrent = versionId === currentVersionId;
          return <button
            key={versionId}
            className={`caption-version ${isActive ? "active" : ""}`}
            aria-pressed={isActive}
            title={`View ${str(version.name, `version ${version.version_number}`)}`}
            onClick={() => { setSelectedVersionId(versionId); setViewingDraft(false); setSelected([]); setPreview(null); }}
          >
            <span className="caption-version-number">v{str(version.version_number)}</span>
            <span className="caption-version-body">
              <strong>{str(version.name, "Imported captions")}</strong>
              <small>{str(version.item_count, "0")} images · {str(version.caption_format, "text")} captions</small>
            </span>
            <span className={`caption-version-state ${isCurrent ? "current" : ""}`}>{isCurrent ? "Current" : str(version.status, "Published")}</span>
          </button>;
        })}
        {draft && <button className={`caption-version ${activeDraft ? "active" : ""}`} aria-pressed={Boolean(activeDraft)} title="Return to the unpublished working copy" onClick={() => { setSelectedVersionId(str(draft.base_version_id)); setViewingDraft(true); setSelected([]); setPreview(null); }}><span className="caption-version-number">Draft</span><span className="caption-version-body"><strong>Working copy</strong><small>{str(rows(draft.items).length, "0")} images · cloned from v{str(versions.find(version => idOf(version) === str(draft.base_version_id))?.version_number, "-")}</small></span><span className="caption-version-state">Editing</span></button>}
      </div>}
    </Panel>
    {rows(data?.training_runs).length > 0 && <Panel title="Training runs using this dataset"><DataTable rows={rows(data?.training_runs)} columns={[["name", "Run"], ["status", "Status"], ["dataset_version_id", "Dataset version"]]} onRow={row => { location.hash = `run/${idOf(row)}`; }} /></Panel>}

    {activeDraft && <Panel title="Batch caption operations">
      <div className="selection-toolbar">
        <div>
          <strong>{selected.length} selected · showing {firstVisible}–{lastVisible} of {items.length}</strong>
          <span className="vela-meta" aria-live="polite">Batch scope: {batchScope === "visible" ? `${visibleItemIds.length} visible items` : batchScope === "selected" ? `${selected.length} selected items` : `all ${items.length} draft items`}.</span>
        </div>
        <div className="actions">
          <button onClick={() => setSelected(previous => [...new Set([...previous, ...visibleItemIds])])} disabled={!visibleItemIds.length || visibleItemIds.every(itemId => selected.includes(itemId))}><Check size={14} />Select visible {visibleItemIds.length}</button>
          <button onClick={() => setSelected(items.map(item => idOf(item)).filter(Boolean))} disabled={!items.length || selected.length === items.length}><Check size={14} />Select all {items.length}</button>
          <button onClick={() => setSelected([])} disabled={!selected.length}><X size={14} />Clear selection</button>
          <button onClick={() => void discardDraft()} disabled={busy === "discard" || captionBusy}><Trash2 size={14} />{busy === "discard" ? "Discarding..." : "Discard draft"}</button>
        </div>
      </div>
      <Field label="Batch scope" hint="Visible affects only this page; Selected follows checked images across pages; Entire draft includes every image.">
        <Dropdown aria-label="Batch scope" value={batchScope} onChange={value => setBatchScope(value as BatchScope)} options={[
          { value: "visible", label: `Visible page (${visibleItemIds.length})` },
          { value: "selected", label: `Selected (${selected.length})`, disabled: !selected.length },
          { value: "all", label: `Entire draft (${items.length})` },
        ]} />
      </Field>
      <div className="form-grid caption-operation-fields">
        <Field label="Caption method" hint="Choose manual editing or generate captions with a vision-capable AI model."><Dropdown aria-label="Caption method" value={captionMethod} onChange={value => setCaptionMethod(value as "manual" | "ai")} options={[{ value: "manual", label: "Manual edit" }, { value: "ai", label: "AI image captions" }]} /></Field>
        <Field label="Caption format" hint="Applies one format consistently across this working draft."><Dropdown aria-label="Caption format" value={captionFormat} disabled={captionBusy} onChange={value => void updateCaptionFormat(value as "text" | "json")} options={[{ value: "text", label: "Text" }, { value: "json", label: "JSON" }]} /></Field>
      </div>
      {captionMethod === "ai" && <div className="caption-generation-fields">
        <div className="form-grid">
          <Field label="1. Provider" hint="The saved provider from Settings.">
            <input aria-label="Caption provider" readOnly value={captionProvider} />
          </Field>
          <Field label="2. Vision model" hint={captionCatalogModels.length ? `${visibleCaptionModelOptions.length} of ${captionCatalogModels.length} caption-capable models shown.` : configuredCaptionModel ? `Configured default: ${configuredCaptionModel}` : "Choose a model returned by the provider API."}>
            <div className="model-catalog-picker">
              <input type="search" aria-label="Filter caption models" value={captionModelFilter} onChange={event => setCaptionModelFilter(event.currentTarget.value)} placeholder={`Filter ${captionCatalogModels.length || "vision"} models…`} />
              <Dropdown aria-label="Caption model" value={captionModel || undefined} placeholder={captionCatalog.loading ? "Loading models…" : normalizedCaptionModelFilter && visibleCaptionModelOptions.length === 0 ? "No matching models" : "Select a vision model…"} options={visibleCaptionModelOptions} onChange={value => { setCaptionModel(value); setCaptionModelFilter(""); }} required />
            </div>
          </Field>
          <Field label="3. Prompt preset">
            <Dropdown aria-label="Caption preset" value={captionPreset} onChange={value => {
              setCaptionPreset(value);
              const preset = value === "__visible_scene" ? { name: "Visible scene", prompt: DEFAULT_VISIBLE_SCENE_CAPTION_PROMPT } : captionPresets.find(item => str(item.name) === value);
              if (preset) { setCaptionPresetName(str(preset.name)); setCaptionPrompt(str(preset.prompt)); }
            }} options={[{ value: "", label: "Custom prompt" }, { value: "__visible_scene", label: "Visible scene (template)" }, ...captionPresets.map(preset => ({ value: str(preset.name), label: str(preset.name) }))]} />
          </Field>
        </div>
        <Notice error={captionCatalog.error} loading={captionCatalog.loading} empty={!captionCatalog.loading && !captionCatalog.error && captionModelOptions.length === 0} emptyText="No caption-capable models were returned." emptyHint="Save the provider and API key in Settings, then reload the model list." />
        <div className="actions caption-catalog-actions"><button type="button" onClick={() => void captionCatalog.reload()} disabled={captionCatalog.loading}><RefreshCw size={14} />{captionCatalog.loading ? "Loading models…" : "Reload models"}</button><a className="button" href="#/settings"><Settings2 size={14} />Provider settings</a></div>
        <Field label="Caption prompt" hint="Tell the model which visible details to include. JSON format additionally requires a JSON-only response."><textarea aria-label="Caption prompt" value={captionPrompt} onChange={event => setCaptionPrompt(event.target.value)} /></Field>
        <div className="form-grid">
          <Field label="Preset name" hint="Saving the same name replaces its prompt."><input aria-label="Caption preset name" value={captionPresetName} onChange={event => setCaptionPresetName(event.target.value)} placeholder="e.g. Visible scene" /></Field>
          <div className="actions caption-preset-actions"><button type="button" onClick={() => void saveCaptionPreset()} disabled={!captionPresetName.trim() || !captionPrompt.trim() || captionPresetBusy}>{captionPresetBusy ? "Saving..." : "Save preset"}</button><button type="button" onClick={() => void deleteCaptionPreset()} disabled={!captionPreset || captionPreset === "__visible_scene" || captionPresetBusy}>Remove preset</button></div>
        </div>
        {captionGenerationReview === captionGenerationFingerprint && <section className="vela-section caption-generation-review" role="region" aria-label="Caption generation review">
          <h3>Review caption generation</h3>
          <p><strong>{batchTargetCount} provider request{batchTargetCount === 1 ? "" : "s"}</strong> will send the selected image data to {captionProvider} using {captionModel}. Generated captions will remain in this working draft until publication.</p>
          <dl className="details"><dt>Scope</dt><dd>{batchScope === "all" ? `Entire draft (${items.length})` : batchScope === "selected" ? `Selected images (${selected.length})` : `Visible page (${visibleItemIds.length})`}</dd><dt>Format</dt><dd>{captionFormat.toUpperCase()}</dd><dt>Prompt</dt><dd>{captionPrompt.trim()}</dd></dl>
          <div className="actions"><button className="primary" onClick={() => void generateCaptions()} disabled={captionBusy || Boolean(busy)}><Sparkles size={14} />Generate reviewed captions</button><button onClick={() => setCaptionGenerationReview("")}>Cancel</button></div>
        </section>}
        {captionProgress && <div className="caption-progress" role="status" aria-live="polite">
          <div><strong>Captioning {captionProgress.filename}</strong><span>{captionProgress.completed} of {captionProgress.total} complete</span></div>
          <progress aria-label="Caption generation progress" value={captionProgress.completed} max={captionProgress.total} />
        </div>}
        <div className="actions"><button className="primary" onClick={() => setCaptionGenerationReview(captionGenerationFingerprint)} disabled={!captionPrompt.trim() || !captionModel.trim() || captionBusy || Boolean(busy) || (!batchTargetCount)} title={!captionPrompt.trim() ? "Enter a prompt before generating captions" : !captionModel.trim() ? "Select a vision-capable model or configure the default model in Settings" : !batchTargetCount ? "Select at least one image or choose all draft items" : undefined}><Sparkles size={14} />Review caption generation</button><span className="muted">{batchScope === "all" ? `Scope: all ${items.length} draft items.` : batchScope === "selected" ? `Scope: ${selected.length} selected items.` : `Scope: ${visibleItemIds.length} visible items.`}</span></div>
      </div>}
      <div className="form-grid">
        <Field label="Find"><input value={find} onChange={event => setFind(event.target.value)} placeholder="Text to replace" /></Field>
        <Field label="Replace with"><input value={replace} onChange={event => setReplace(event.target.value)} placeholder="Replacement text" /></Field>
        <Field label="Add word"><input value={word} onChange={event => setWord(event.target.value)} placeholder="Trigger or tag word" /></Field>
      </div>
      <div className="actions caption-preview-actions">
        <button onClick={() => void previewOperation("replace")} disabled={!find || !batchTargetCount || Boolean(busy || captionBusy)} title={!find ? "Enter text to find before previewing" : !batchTargetCount ? "Choose a scope containing at least one image" : undefined}><Search size={14} />{busy === "preview-replace" ? "Previewing..." : "Preview replacement"}</button>
        <button onClick={() => void previewOperation("add_word")} disabled={!word || !batchTargetCount || Boolean(busy || captionBusy)} title={!word ? "Enter a word before previewing" : undefined}><Plus size={14} />{busy === "preview-add_word" ? "Previewing..." : "Preview add word"}</button>
        <button onClick={() => void previewOperation("remove_word")} disabled={!word || !batchTargetCount || Boolean(busy || captionBusy)} title={!word ? "Enter a word before previewing" : undefined}><X size={14} />{busy === "preview-remove_word" ? "Previewing..." : "Preview remove word"}</button>
        <button onClick={() => void previewOperation("set_included", true)} disabled={!batchTargetCount || Boolean(busy || captionBusy)}><Check size={14} />{busy === "preview-set_included" ? "Previewing..." : "Preview include"}</button>
        <button onClick={() => void previewOperation("set_included", false)} disabled={!batchTargetCount || Boolean(busy || captionBusy)}><X size={14} />{busy === "preview-set_included" ? "Previewing..." : "Preview exclude"}</button>
      </div>
      {preview && <section className="vela-section caption-operation-preview" role="status" aria-live="polite" aria-label="Operation preview">
        <div className="vela-section-heading"><h2>Operation preview</h2><button className="vela-text-button" onClick={() => setPreview(null)}>Dismiss</button></div>
        <p><strong>{str(preview.changed, "0")} changes</strong> across {str(preview.matched, "0")} matched items in the {batchScope === "all" ? "entire draft" : batchScope} scope.</p>
        <Json value={{ target_item_ids: preview.target_item_ids, changes: preview.changes, truncated: preview.truncated }} />
        <div className="actions"><button className="primary" onClick={() => void applyPreview()} disabled={Boolean(busy || captionBusy)}><Sparkles size={14} />{busy.startsWith("apply-") ? "Applying preview..." : "Apply this preview"}</button></div>
      </section>}
      <details className="vela-history-disclosure caption-operation-history">
        <summary>Operation history ({rows(operationHistory.data).length})</summary>
        <Notice error={operationHistory.error} loading={operationHistory.loading} empty={!operationHistory.loading && !rows(operationHistory.data).length} emptyText="No caption operations yet" emptyHint="Preview and apply a batch edit to create reversible history." />
        {!!rows(operationHistory.data).length && <ol>{rows(operationHistory.data).map(history => <li key={idOf(history)}>
          <span><strong>{str(history.operation).replaceAll("_", " ")}</strong><small>{str(history.after_count, "0")} changed · {str(history.created_at)}</small></span>
          {Boolean(history.can_undo) && <button onClick={() => void undoOperation(idOf(history))} disabled={Boolean(busy || captionBusy)}>{busy === `undo-${idOf(history)}` ? "Undoing..." : "Undo"}</button>}
          {Boolean(history.undone) && <Status value="undone" />}
        </li>)}</ol>}
      </details>
      <Form submit="Publish version" onSubmit={async form => {
        await api(`/api/dataset-drafts/${activeDraft.id}/publish`, jsonBody({ name: form.get("version_name") }));
        activeDraftResource.setData(null); setDraft(null); setViewingDraft(false); setPreview(null); setSelected([]); setSelectedVersionId(""); setMessage("Dataset version published.");
        await Promise.all([resource.reload(), versionsResource.reload()]);
      }}><Field label="Version name" hint="Use a short label that explains the caption change."><input name="version_name" required placeholder="e.g. Trigger word cleanup" /></Field></Form>
    </Panel>}

    <Panel title="Images and captions">
      <Notice error={!activeDraft ? publishedItems.error : undefined} loading={!activeDraft && publishedItems.loading} empty={!publishedItems.loading && !totalItems} />
      {!publishedItems.loading && !totalItems && Boolean(data?.empty_reason) && <div className="vela-notice" role="status"><strong>Dataset is empty</strong><span>{str(data?.empty_reason)}</span></div>}
      {!!visibleItems.length && <div className="caption-list">{visibleItems.map((item, index) => <CaptionItem key={idOf(item) || index} item={item} selected={selected.includes(idOf(item))} onSelect={checked => setSelected(previous => {
        const itemId = idOf(item);
        return checked ? [...new Set([...previous, itemId])] : previous.filter(value => value !== itemId);
      })} datasetId={id} projectId={str(data?.project_id)} draftId={str(activeDraft?.id, "")} onSaved={created => {
        if (!activeDraft) return;
        const next = [...items]; next[(page - 1) * pageSize + index] = { ...item, ...created }; setDraft({ ...activeDraft, items: next }); void operationHistory.reload();
      }} />)}</div>}
      {!!totalItems && <Pagination page={page} pageSize={pageSize} total={totalItems} onPage={setPage} onPageSize={setPageSize} />}
    </Panel>
  </Page>;
}

function CaptionItem({ item, datasetId, projectId, draftId, selected, onSelect, onSaved }: { item: Row; datasetId: string; projectId: string; draftId: string; selected: boolean; onSelect: (checked: boolean) => void; onSaved: (row: Row) => void }) {
  const presentation = captionPresentation(item, str(item.filename ?? item.name, "Dataset image"));
  const [caption, setCaption] = useState(presentation.rendered);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const assetId = item.asset_revision_id ?? item.asset_id ?? item.id;
  const filename = str(item.filename ?? item.name, "Dataset image");
  useEffect(() => setCaption(captionPresentation(item, filename).rendered), [item.caption, item.caption_format, filename]);
  return <article className={`caption-item ${selected ? "selected" : ""}`}>
    <div className="caption-image">
      <label className="caption-select"><input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} aria-label={`Select ${filename}`} /></label>
      <a href={galleryOpenHref({ ...item, dataset_id: datasetId, project_id: projectId }, "dataset_image")} title={`Open ${filename} in the image reviewer`}><AssetImage assetRevisionId={String(assetId)} alt={presentation.alt} loading="lazy" /><span>Open image</span></a>
    </div>
    <div className="caption-copy">
      <div className="caption-heading"><strong>{filename}</strong><Status value={presentation.format.toUpperCase()} /></div>
      {presentation.format === "json" && !draftId
        ? <pre className="caption-json" aria-label={`JSON caption for ${filename}`}>{presentation.rendered}</pre>
        : <textarea className={presentation.format === "json" ? "caption-json-editor" : undefined} aria-label={`${presentation.format === "json" ? "JSON caption" : "Caption"} for ${filename}`} value={caption} readOnly={!draftId} title={!draftId ? "Create a new version to edit this caption" : undefined} onChange={event => setCaption(event.target.value)} />}
      {item.availability === "missing" && <div className="vela-notice vela-notice-error" role="alert">{str(item.availability_error, "The source image object is missing.")}</div>}
      <label className="caption-included" title={!draftId ? "Create a new version to change inclusion" : undefined}><input type="checkbox" checked={Boolean(item.included ?? true)} disabled={!draftId || saving} onChange={async event => {
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

const LIVE_CONNECTION_LABELS: Record<string, string> = {
  connecting: "Connecting",
  connected: "Live",
  reconnecting: "Reconnecting",
  stopped: "Updates stopped",
};

function formatLiveElapsed(value: unknown) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "Unavailable";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = Math.floor(seconds % 60);
  return `${hours ? `${hours}h ` : ""}${minutes ? `${minutes}m ` : ""}${remainder}s`;
}

function summarizeLiveUploads(value: unknown, fallback: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const counts = Object.entries(value as Row)
      .map(([state, count]) => [state.toLowerCase(), Number(count)] as const)
      .filter(([, count]) => Number.isFinite(count) && count > 0);
    if (counts.length) {
      const total = counts.reduce((sum, [, count]) => sum + count, 0);
      if (counts.some(([state]) => /fail|error/.test(state))) return "Failed";
      const uploaded = counts
        .filter(([state]) => /uploaded|complete|success|verified/.test(state))
        .reduce((sum, [, count]) => sum + count, 0);
      return uploaded === total
        ? `${uploaded}/${total} uploaded`
        : `${uploaded}/${total} uploaded · ${total - uploaded} pending`;
    }
  }
  const uploads = rows(value);
  if (!uploads.length) return value === fallback ? "Unavailable" : summarizeLiveUploads(fallback, fallback);
  const states = uploads.map(upload => str(upload.status ?? upload.state ?? upload.upload_status, "pending").toLowerCase());
  if (states.some(state => /fail|error/.test(state))) return "Failed";
  const uploaded = states.filter(state => /uploaded|complete|success|verified/.test(state)).length;
  if (uploaded === uploads.length) return `${uploaded}/${uploads.length} uploaded`;
  return `${uploaded}/${uploads.length} uploaded · ${uploads.length - uploaded} pending`;
}

export function RunScreen({ id }: { id: string }) {
  const run = useResource<Row>(`/api/runs/${id}`);
  const live = useRunLive(id);
  const samples = useResource<unknown>(`/api/runs/${id}/samples`);
  const checkpointsResource = useResource<unknown>(`/api/runs/${id}/checkpoints`);
  const config = useResource<Row>(`/api/runs/${id}/config`);
  const runView: Row = { ...(run.data ?? {}), ...(live.data ?? {}) };
  const latestLoss = runView.latest_loss && typeof runView.latest_loss === "object" && !Array.isArray(runView.latest_loss) ? runView.latest_loss as Row : null;
  const lossMetricName = str(latestLoss?.name, "");
  const metrics = useResource<LossMetrics>(lossMetricName ? `/api/runs/${id}/metrics${query({ name: lossMetricName })}` : null);
  const learningRateMetrics = useResource<LossMetrics>(`/api/runs/${id}/metrics?name=learning_rate`);
  const lineage = useResource<unknown>(`/api/lineage${query({ subject_type: "training_run", subject_id: id, depth: 3 })}`);
  const rawCheckpoints = rows(checkpointsResource.data);
  const enrichedCheckpoints = rows(runView.checkpoints);
  const checkpoints = enrichedCheckpoints.length
    ? enrichedCheckpoints.map(checkpoint => ({ ...rawCheckpoints.find(raw => idOf(raw) === idOf(checkpoint)), ...checkpoint }))
    : rawCheckpoints;
  const [selectedCheckpointId, setSelectedCheckpointId] = useState("");
  const selectedCheckpoint = checkpoints.find(checkpoint => idOf(checkpoint) === selectedCheckpointId) ?? null;
  const [datasetId, setDatasetId] = useState("");
  const [datasetLoading, setDatasetLoading] = useState(false);
  const [datasetError, setDatasetError] = useState("");
  const [refreshReviewed, setRefreshReviewed] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const sampleRows = rows(samples.data);
  const sampleSteps = [...new Set(sampleRows.map(sample => str(sample.training_step, "unassigned")))];
  const datasetVersionId = str(runView.dataset_version_id, "");
  const runProjectId = str(runView.project_id, "");
  const linkedDatasetId = str(runView.dataset_id, "");
  const uploadSummary = summarizeLiveUploads(runView.uploads, runView.upload_status);
  const lossRevision = `${lossMetricName}:${str(latestLoss?.step, "")}:${str(latestLoss?.value ?? latestLoss?.value_text, "")}`;
  const sampleRevision = `${str(runView.sample_count, "")}:${rows(runView.uploads).map(upload => str(upload.status ?? upload.state ?? upload.upload_status)).join(",")}`;
  const observedLossRevision = useRef("");
  const observedSampleRevision = useRef("");

  useEffect(() => {
    let active = true;
    setDatasetId(""); setDatasetError("");
    if (linkedDatasetId) { setDatasetId(linkedDatasetId); setDatasetLoading(false); return () => { active = false; }; }
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
  }, [datasetVersionId, linkedDatasetId, runProjectId]);

  useEffect(() => {
    if (!live.revision) return;
    if (observedLossRevision.current && observedLossRevision.current !== lossRevision) void metrics.reload();
    if (observedSampleRevision.current && observedSampleRevision.current !== sampleRevision) void samples.reload();
    observedLossRevision.current = lossRevision;
    observedSampleRevision.current = sampleRevision;
  }, [live.revision, lossRevision, sampleRevision, metrics.reload, samples.reload]);

  async function refreshRun() {
    setBusy("refresh"); setMessage(""); setActionError("");
    try {
      const job = await api<Row>(`/api/runs/${id}/refresh`, jsonBody({}));
      await Promise.all([run.reload(), samples.reload(), checkpointsResource.reload(), metrics.reload(), learningRateMetrics.reload()]);
      setMessage(`Run refresh queued${job.id ? ` as job ${job.id}` : ""}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }


  const lastEventDate = new Date(str(runView.last_event_at, ""));
  const lastEventText = Number.isNaN(lastEventDate.valueOf()) ? "Unavailable" : lastEventDate.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
  const latestLossValue = str(latestLoss?.value_text ?? latestLoss?.value ?? metrics.data?.final_value, "Unavailable");
  if (!run.data && run.loading) return <Page title="Training run" subtitle="Loading imported run details"><section aria-busy="true"><Notice loading /></section></Page>;
  if (!run.data && run.error) return <Page title="Run unavailable" subtitle={`Run ${id}`}><Notice error={run.error} /><div className="actions"><a className="button vela-button" href="#/runs">Back to runs</a><button onClick={() => void run.reload()}>Retry</button></div></Page>;
  return <Page title={str(runView.name, "Training run")} subtitle={`${str(runView.base_model, "Unknown base")} · ${str(runView.dataset_version_name, "No dataset")} · ${str(runView.trainer, "Unknown trainer")}`} actions={<>
    {datasetId ? <a className="button vela-button" href={`#/dataset/${datasetId}`}><Database size={15} />Dataset</a> : <button disabled title={datasetError || (datasetLoading ? "Resolving the linked dataset version" : "This run does not reference an available dataset version")}><Database size={15} />{datasetLoading ? "Finding dataset..." : "Dataset unavailable"}</button>}
    <a className="button vela-button" href={`#/samples/${id}?return=${encodeURIComponent(`run/${id}`)}`}><FileImage size={15} />Samples</a>
    {runView.project_id ? <a className="button vela-button" href={`#/transfers?mode=export&project=${str(runView.project_id)}`}><Download size={15} />Export project</a> : <button disabled title="This run is not attached to an exportable project"><Download size={15} />Export unavailable</button>}
    <button className="vela-button vela-button-primary" onClick={() => void refreshRun()} disabled={!refreshReviewed || busy === "refresh"} title={!refreshReviewed ? "Review and acknowledge the refresh scope below first" : undefined}><RefreshCw size={15} />{busy === "refresh" ? "Refreshing..." : "Refresh import"}</button>
  </>}>
    <ActionMessage message={message} error={actionError} />
    <section className="run-refresh-review" aria-labelledby="run-refresh-heading"><div><h2 id="run-refresh-heading">Refresh review</h2><p>Re-scan this run's existing source prefix and reconcile checkpoints, samples, and metrics. This does not start training or contact a generation provider.</p></div><label><input type="checkbox" checked={refreshReviewed} onChange={event => setRefreshReviewed(event.target.checked)} /> I reviewed the source scope and repeated import work.</label></section>
    <ObjectImageStrip assets={sampleRows.slice().reverse()} label="Latest training samples" showTrainingDetails />
    {live.error && <div className="vela-notice" role="status" aria-live="polite"><strong>{LIVE_CONNECTION_LABELS[live.connection]}</strong><span>{live.error}</span></div>}
    {(run.data || live.data) && <div className="run-summary live-run-summary vela-context-stats" aria-label="Live training status">
      <div className="vela-stat"><span>Status</span><Status value={runView.status} /></div>
      <div className="vela-stat"><span>Connection</span><span className="vela-status" data-state={live.connection === "connected" ? "success" : live.connection === "reconnecting" ? "warning" : "neutral"} aria-label={`Live connection: ${LIVE_CONNECTION_LABELS[live.connection]}`} aria-live="polite">{LIVE_CONNECTION_LABELS[live.connection]}</span></div>
      <div className="vela-stat"><span>Step</span><strong>{str(runView.current_step, "Unavailable")}</strong></div>
      <div className="vela-stat"><span>{lossMetricName || "Latest loss"}</span><strong>{latestLossValue}</strong></div>
      <div className="vela-stat"><span>Learning rate</span><strong>{scientificNotation(runView.learning_rate ?? (runView.normalized_config as Row | undefined)?.learning_rate)}</strong></div>
      <div className="vela-stat"><span>Elapsed</span><strong>{formatLiveElapsed(runView.elapsed_seconds)}</strong></div>
      <div className="vela-stat"><span>Last event</span><strong>{lastEventText === "Unavailable" ? lastEventText : <time dateTime={str(runView.last_event_at)}>{lastEventText}</time>}</strong></div>
      <div className="vela-stat"><span>Uploads</span><Status value={uploadSummary} /></div>
      <div className="vela-stat"><span>Checkpoints</span><strong>{checkpoints.length}</strong></div>
      <div className="vela-stat"><span>Samples</span><strong>{str(runView.sample_count, String(sampleRows.length))}</strong></div>
      <div className="vela-stat"><span>LoRA rank</span><strong>{str(runView.lora_rank ?? runView.network_dim, "-")}</strong></div>
    </div>}
    {runView.source_status === "failed" && runView.usable_output === true && <div className="vela-notice" role="status"><strong>Usable training output recovered</strong><span>The trainer process reported “{str(runView.source_message, "failed termination")}”, but the DAM found {checkpoints.length} usable checkpoints and retained the original termination details in normalized configuration.</span></div>}

    <Panel title="Training metrics">
      <Notice error={metrics.error || learningRateMetrics.error} loading={metrics.loading || learningRateMetrics.loading} />
      <div className="training-metric-summary"><div><span>{lossMetricName || "Latest loss"}</span><strong>{latestLossValue}</strong></div><div><span>Configured learning rate</span><strong>{scientificNotation(runView.learning_rate ?? (runView.normalized_config as Row | undefined)?.learning_rate)}</strong></div><div><span>Loss points</span><strong>{normalizeLossPoints(metrics.data?.points).length}</strong></div><div><span>LR points</span><strong>{normalizeLossPoints(learningRateMetrics.data?.points).length}</strong></div></div>
      <div className="training-chart-grid"><section><header><strong>{lossMetricName || "Training loss"}</strong><small>Lower is generally better</small></header><LossGraph points={metrics.data?.points} /></section><section><header><strong>Learning rate</strong><small>Optimizer schedule</small></header>{normalizeLossPoints(learningRateMetrics.data?.points).length ? <FlintMetricChart points={normalizeLossPoints(learningRateMetrics.data?.points)} label="Learning rate" /> : <div className="loss-graph-empty" role="status"><strong>No learning-rate series available</strong><small>The configured value remains visible above.</small></div>}</section></div>
    </Panel>

    <Panel title="Run checkpoint artifacts">
      <Notice error={!enrichedCheckpoints.length ? checkpointsResource.error : undefined} loading={checkpointsResource.loading && !enrichedCheckpoints.length} empty={!checkpointsResource.loading && !checkpoints.length} emptyText="No checkpoint artifacts" emptyHint="No checkpoint files were indexed for this run." />
      {!!checkpoints.length && <div className="checkpoint-timeline">{checkpoints.map(checkpoint => {
        const sample = sampleRows.find(item => str(item.checkpoint_id) === idOf(checkpoint)) ?? sampleRows.find(item => str(item.training_step) === str(checkpoint.step));
        const readiness = checkpoint.readiness_summary && typeof checkpoint.readiness_summary === "object" ? checkpoint.readiness_summary as Row : {};
        const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
        return <button className={selectedCheckpointId === idOf(checkpoint) ? "active" : ""} aria-pressed={selectedCheckpointId === idOf(checkpoint)} key={idOf(checkpoint)} onClick={() => setSelectedCheckpointId(idOf(checkpoint))}>{sample && <AssetImage assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id)} alt="" loading="lazy" />}<strong>Step {str(checkpoint.step)}</strong><Status value={local.status ?? checkpoint.state} /><span>{str(checkpoint.filename ?? checkpoint.name)}</span></button>;
      })}</div>}
      {selectedCheckpoint && (() => {
        const readiness = selectedCheckpoint.readiness_summary && typeof selectedCheckpoint.readiness_summary === "object" ? selectedCheckpoint.readiness_summary as Row : {};
        const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
        const registered = selectedCheckpoint.registered_version && typeof selectedCheckpoint.registered_version === "object" ? selectedCheckpoint.registered_version as Row : null;
        return <div className="checkpoint-detail"><dl className="details"><dt>Artifact file</dt><dd>{str(selectedCheckpoint.filename ?? selectedCheckpoint.name)}</dd><dt>Size</dt><dd>{formatBytes(selectedCheckpoint.size)}</dd><dt>Local readiness</dt><dd><Status value={local.status ?? selectedCheckpoint.state} /> {str(local.reason)}</dd><dt>Registered version</dt><dd>{registered ? <a href={`#/model-version/${idOf(registered)}`}>{str(registered.name)}</a> : "Not registered"}</dd></dl><div className="actions"><a className="button vela-button" href={`#/checkpoint/${idOf(selectedCheckpoint)}`}><ExternalLink size={14} />Open checkpoint artifact</a>{registered && <a className="button vela-button" href={`#/model-version/${idOf(registered)}`}>Open registered version</a>}</div></div>;
      })()}
    </Panel>

    <Panel title="Samples by checkpoint">
      <Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />
      {!samples.loading && !sampleRows.length && <div className="vela-notice" role="status"><strong>No sample images were archived</strong><span>The run can still contain valid checkpoints and metrics; its source prefix contains no sample-image artifacts to import.</span></div>}
      {!!sampleSteps.length && <div className="sample-step-strip">{sampleSteps.map(step => <a href={`#/samples/${id}?step=${encodeURIComponent(step)}&return=${encodeURIComponent(`run/${id}`)}`} key={step}><strong>Step {step}</strong><span>{sampleRows.filter(sample => str(sample.training_step, "unassigned") === step).length} images</span></a>)}</div>}
      {!!sampleRows.length && <AssetGrid assets={sampleRows.slice(0, 12)} />}
    </Panel>

    <div className="dashboard-lower vela-dashboard-grid">
      <Panel title="Normalized configuration"><Notice error={config.error} loading={config.loading} />{config.data && <ConfigDetails value={(config.data.normalized ?? config.data.normalized_config ?? config.data) as Row} datasetId={datasetId} />}</Panel>
      <Panel title="Training config"><Notice error={config.error} loading={config.loading} />{!config.loading && <details className="config-disclosure"><summary>Show raw training configuration</summary><Json value={config.data?.raw ?? config.data?.raw_config ?? run.data?.raw_config ?? (config.data?.normalized as Row | undefined)?.training_config ?? { state: "Not imported" }} /></details>}</Panel>
    </div>
    <Panel title="Typed lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} emptyText="No lineage indexed" emptyHint="No persisted or source-derived relationships were found for this run." />{!!rows(lineage.data).length && <LineageList value={lineage.data} />}</Panel>
  </Page>;
}

function ConfigDetails({ value, datasetId }: { value: Row; datasetId?: string }) {
  const entries = Object.entries(value).filter(([key]) => key !== "training_config");
  return <><dl className="details">{entries.slice(0, 14).map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{key === "learning_rate" ? scientificNotation(item) : key === "dataset_sources" && datasetId ? <a href={`#/dataset/${datasetId}`}>{Array.isArray(item) ? item.map(source => String(source).split("/").filter(Boolean).at(-1)).join(", ") : "Open dataset"}</a> : typeof item === "object" ? JSON.stringify(item) : str(item)}</dd></Fragment>)}</dl>{value.training_config && <details className="config-disclosure"><summary>Show complete training config</summary><Json value={value.training_config} /></details>}</>;
}

export function CheckpointScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/checkpoints/${id}`);
  const registeredVersion = resource.data?.registered_version && typeof resource.data.registered_version === "object" ? resource.data.registered_version as Row : null;
  const models = useResource<unknown>(resource.data && !registeredVersion ? "/api/models" : null);
  const lineage = useResource<unknown>(resource.data ? `/api/lineage${query({ subject_type: "checkpoint", subject_id: id, depth: 3 })}` : null);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [hydrating, setHydrating] = useState(false);
  const [hydrationReviewed, setHydrationReviewed] = useState(false);
  const checkpointAsset = resource.data?.asset && typeof resource.data.asset === "object" ? resource.data.asset as Row : {};
  const artifact = resource.data?.artifact && typeof resource.data.artifact === "object" ? resource.data.artifact as Row : {};
  const readiness = resource.data?.readiness_summary && typeof resource.data.readiness_summary === "object" ? resource.data.readiness_summary as Row : {};
  const localReadiness = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
  const remoteReadiness = readiness.remote && typeof readiness.remote === "object" ? readiness.remote as Row : {};
  const dataset = resource.data?.dataset && typeof resource.data.dataset === "object" ? resource.data.dataset as Row : null;
  const storage = rows(artifact.storage);
  const checkpointProjectId = str(checkpointAsset.project_id, "");
  const modelRows = rows(models.data).filter(model => !checkpointProjectId || str(model.project_id, "") === checkpointProjectId);
  const canHydrate = Boolean(localReadiness.can_hydrate);

  async function hydrate() {
    if (!hydrationReviewed || !canHydrate) return;
    setHydrating(true); setActionError(""); setMessage("");
    try {
      const job = await api<Row>(`/api/checkpoints/${id}/hydrate`, jsonBody({}));
      await resource.reload();
      setMessage(`Hydration queued${job.id ? ` as job ${job.id}` : ""}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setHydrating(false); }
  }

  if (!resource.data && resource.loading) return <Page title="Checkpoint" subtitle="Loading checkpoint artifact"><section aria-busy="true"><Notice loading /></section></Page>;
  if (!resource.data && resource.error) return <Page title="Checkpoint unavailable" subtitle={`Checkpoint ${id}`}><Notice error={resource.error} /><div className="actions"><a className="button vela-button" href="#/runs">Back to runs</a><button onClick={() => void resource.reload()}>Retry</button></div></Page>;

  return <Page title={`Checkpoint step ${str(resource.data?.step, "")}`} subtitle={`${str(resource.data?.run_name, "Training run")} · Run artifact`} actions={canHydrate
    ? <button className="vela-button vela-button-primary" onClick={() => void hydrate()} disabled={hydrating || !hydrationReviewed} title={!hydrationReviewed ? "Review the transfer consequence below before hydrating" : undefined}><Download size={15} />{hydrating ? "Queuing..." : "Hydrate locally"}</button>
    : <span className="checkpoint-readiness-static" role="status"><Status value={localReadiness.status ?? resource.data?.state} />{str(localReadiness.reason)}</span>}>
    <ActionMessage message={message} error={actionError} />
    <Panel title="Run checkpoint artifact"><dl className="details"><dt>Filename</dt><dd>{str(artifact.filename, "Missing asset record")}</dd><dt>Checkpoint ID</dt><dd>{str(artifact.checkpoint_id)}</dd><dt>Asset ID</dt><dd>{str(artifact.asset_id)}</dd><dt>Revision</dt><dd>{str(artifact.checkpoint_revision_id, "No materialized revision")}</dd><dt>Training step</dt><dd>{str(artifact.step)}</dd><dt>Artifact state</dt><dd><Status value={artifact.state} /></dd><dt>Local readiness</dt><dd><Status value={localReadiness.status ?? "unknown"} /> {str(localReadiness.reason)}</dd><dt>Size</dt><dd>{formatBytes(remoteReadiness.size ?? storage[0]?.size)}</dd><dt>Storage</dt><dd>{storage.length ? storage.map(location => `${str(location.provider)} · ${str(location.hydration_state)} · ${str(location.object_key ?? location.relative_path ?? location.uri)}`).join("; ") : "No storage location recorded"}</dd><dt>SHA-256</dt><dd>{str(artifact.sha256, "Not recorded")}</dd><dt>Source run</dt><dd>{resource.data?.run_id ? <a href={`#/run/${str(resource.data.run_id)}`}>{str(resource.data.run_name, "Open run")}</a> : "Unavailable"}</dd><dt>Dataset version</dt><dd>{dataset?.dataset_id ? <a href={`#/dataset/${str(dataset.dataset_id)}`}>{str(dataset.dataset_name)} · {str(dataset.dataset_version_name)}</a> : "Not linked"}</dd></dl><details className="config-disclosure"><summary>Artifact metadata</summary><Json value={artifact.metadata ?? {}} /></details></Panel>
    {canHydrate && <section className="checkpoint-hydration-review" aria-labelledby="checkpoint-hydration-heading"><div><h2 id="checkpoint-hydration-heading">Hydration review</h2><p>Download {formatBytes(remoteReadiness.size ?? storage[0]?.size)} from {str(remoteReadiness.provider, "the recorded remote source")} into managed local storage. Existing source data is not deleted.</p></div><label><input type="checkbox" checked={hydrationReviewed} onChange={event => setHydrationReviewed(event.target.checked)} /> I reviewed the artifact size, source, and local-storage consequence.</label></section>}
    {registeredVersion ? <Panel title="Registered model version"><p>This checkpoint artifact is already registered as one intentional reusable version.</p><dl className="details"><dt>Version</dt><dd><a href={`#/model-version/${idOf(registeredVersion)}`}>{str(registeredVersion.name)}</a></dd><dt>Model</dt><dd>{registeredVersion.model_id ? <a href={`#/model/${str(registeredVersion.model_id)}`}>{str(registeredVersion.model_name, "Open model")}</a> : str(registeredVersion.model_name)}</dd><dt>Lifecycle</dt><dd><Status value={registeredVersion.lifecycle_state} /></dd></dl><a className="button vela-button" href={`#/model-version/${idOf(registeredVersion)}`}>Open registered version</a></Panel> : <Panel title="Register model version">
      <Notice error={models.error} loading={models.loading} />
      {!models.loading && !modelRows.length && <div className="vela-empty"><span>No compatible model</span><small>Create a model in this checkpoint's project before registering a version.</small><a className="button vela-button" href={`#/models${query({ project: checkpointProjectId })}`}><Plus size={14} />Create model</a></div>}
      {!!modelRows.length && <Form submit="Register version" onSubmit={async form => {
        if (form.get("registration_review") !== "on") throw new Error("Review and acknowledge the registration consequence.");
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
      }}><p>Registration creates a reusable model version tied to this immutable checkpoint revision. It does not upload or hydrate the artifact.</p><Field label="Model"><SelectRecords name="model_id" records={modelRows} required /></Field><Field label="Version name"><input name="name" required placeholder={`Step ${str(resource.data?.step, "checkpoint")}`} /></Field><Field label="Base model"><input name="base_model" placeholder="e.g. FLUX.1-dev" /></Field><Field label="Trigger words" hint="Separate multiple trigger words with commas."><input name="trigger_words" placeholder="subject_token, style_token" /></Field><Field label="Notes"><textarea name="notes" placeholder="Training observations or intended evaluation scope" /></Field><label><input name="registration_review" type="checkbox" required /> I reviewed the checkpoint revision, model family, and reusable-version consequence.</label></Form>}
    </Panel>}
    <Panel title="Typed lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} emptyText="No lineage indexed" emptyHint="No source-derived relationships were found for this checkpoint." />{!!rows(lineage.data).length && <LineageList value={lineage.data} />}</Panel>
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
      }}><Field label="Project"><Dropdown aria-label="Project" name="project_id" required defaultValue={projectId} options={[{ value: "", label: "Select..." }, ...projectRows.map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} /></Field><Field label="Name"><input name="name" required placeholder="Model name" /></Field><Field label="Description"><textarea name="description" placeholder="Subject, style, and intended use" /></Field></Form>}
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
  const latestVersion = versions[0] ?? {};
  const versionRunIds = new Set(versions.flatMap(version => [str(version.checkpoint_run_id), str((version.readiness as Row | undefined)?.training_run_id)]).filter(Boolean));
  const projectRuns = rows(runs.data);
  const relatedRuns = projectRuns.filter(run => versionRunIds.has(idOf(run)));
  const modelTrack = str(resource.data?.name).toLowerCase().includes("render") ? "render" : str(resource.data?.name).toLowerCase().includes("flat") ? "flat" : "";


  if (!resource.data && resource.loading) return <Page title="Model" subtitle="Loading registered model details"><section aria-busy="true"><Notice loading /></section></Page>;
  if (!resource.data && resource.error) return <Page title="Model unavailable" subtitle={`Model ${id}`}><Notice error={resource.error} /><div className="actions"><a className="button vela-button" href="#/models">Back to models</a><button onClick={() => void resource.reload()}>Retry</button></div></Page>;
  const readiness = latestVersion.readiness_summary && typeof latestVersion.readiness_summary === "object" ? latestVersion.readiness_summary as Row : {};
  const falReadiness = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
  const readyForGeneration = Boolean(falReadiness.available);
  return <Page title={str(resource.data?.name, "Model")} subtitle={`${str(latestVersion.base_model, "Base model unknown")} · ${str(project.data?.title ?? project.data?.name, "Project")}`} actions={<>
    {projectId && readyForGeneration ? <><a className="button vela-button vela-button-primary" href={generatorHref("image", { kind: "model", projectId, modelId: id, modelVersionId: idOf(latestVersion) })}><ImagePlus size={15} /> + Image</a><a className="button vela-button" href={`#/evals?project=${projectId}&scope=model&model=${id}&model_version=${idOf(latestVersion)}`}><Sparkles size={15} /> Eval</a><a className="button vela-button" href={`#/grids?project=${projectId}&scope=model&model=${id}&model_version=${idOf(latestVersion)}`}><Grid3X3 size={15} /> Grid</a></> : <button disabled title={projectId ? "Register this version with a compatible FAL endpoint before generation or evaluation" : "The model is not attached to a project"}>Generation unavailable</button>}
    {idOf(latestVersion) ? <a className="button vela-button" href={`#/transfers?mode=export&model_version=${idOf(latestVersion)}`}><Download size={15} />Export version</a> : <button disabled title="Register a model version before exporting"><Download size={15} />Export unavailable</button>}
  </>}>
    <Notice error={resource.error} loading={resource.loading} />
    <ObjectImageStrip assets={rows(preview.data)} label="Latest model outputs" />
    {resource.data && <Panel title="Model record"><Notice error={project.error} loading={project.loading} /><dl className="details"><dt>Description</dt><dd>{str(resource.data.description, "No description")}</dd><dt>Latest trigger words</dt><dd>{Array.isArray(latestVersion.trigger_words) && latestVersion.trigger_words.length ? latestVersion.trigger_words.join(", ") : "None registered"}</dd><dt>Base model</dt><dd>{str(latestVersion.base_model, "Not registered")}</dd><dt>Project</dt><dd>{projectId ? <a href={`#/project/${projectId}`}>{str(project.data?.title ?? project.data?.name, "Open project")}</a> : "Not assigned"}</dd><dt>Versions</dt><dd>{versions.length}</dd></dl></Panel>}

    <Panel title="Training run data">
      <Notice error={runs.error} loading={runs.loading} empty={!runs.loading && !relatedRuns.length} />
      {relatedRuns.map((run, index) => <ModelTrainingRun key={idOf(run)} run={run} modelId={id} track={modelTrack} initiallyExpanded={index === 0} />)}
    </Panel>

    <Panel title="Eval Runs">
      <Notice error={evals.error} loading={evals.loading} empty={!evals.loading && !rows(evals.data).length} />
      {!!rows(evals.data).length && <div className="eval-run-strip">{rows(evals.data).map(evaluation => <a href={`#/eval/${idOf(evaluation)}`} key={idOf(evaluation)}><div className="eval-thumbs">{rows(evaluation.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={String(output.asset_revision_id ?? output.asset_id)} alt="Eval output" />)}</div><strong>{str(evaluation.name, "Eval run")}</strong><span>{str(evaluation.prompt_set_name)} · {str(evaluation.output_count, "0")} outputs</span><Status value={evaluation.status} /></a>)}</div>}
    </Panel>

    <Panel title="Registered model versions">
      <Notice loading={resource.loading} empty={!resource.loading && !versions.length} emptyText="No registered versions" emptyHint="Open a run checkpoint artifact to intentionally register a reusable model version." />
      {!!versions.length && <ModelVersionTable versions={versions} />}
    </Panel>
  </Page>;
}

function ModelVersionTable({ versions }: { versions: Row[] }) {
  return <>
    <div className="model-version-table-wrap" role="region" aria-label="Registered model versions" tabIndex={0}><table className="vela-table model-version-table"><thead><tr><th>Registered version</th><th>Checkpoint artifact</th><th>Step</th><th>Lifecycle</th><th>Local readiness</th><th>FAL readiness</th></tr></thead><tbody>{versions.map(version => {
      const readiness = version.readiness_summary && typeof version.readiness_summary === "object" ? version.readiness_summary as Row : {};
      const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
      const fal = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
      return <tr key={idOf(version)}>
        <td><a className="model-version-link" href={`#/model-version/${idOf(version)}`}><strong>{str(version.name, "Open version")}</strong><small>{idOf(version)}</small></a></td>
        <td>{version.checkpoint_id ? <a href={`#/checkpoint/${str(version.checkpoint_id)}`}>{str(version.filename, "Open checkpoint")}</a> : str(version.filename, "Unavailable")}</td>
        <td>{str(version.checkpoint_step, "Not recorded")}</td>
        <td><Status value={version.lifecycle_state} /></td>
        <td><Status value={local.status ?? "unknown"} /></td>
        <td><Status value={fal.status ?? "not registered"} /></td>
      </tr>;
    })}</tbody></table></div>
    <div className="model-version-mobile-list" aria-label="Registered model version summaries">{versions.map(version => {
      const readiness = version.readiness_summary && typeof version.readiness_summary === "object" ? version.readiness_summary as Row : {};
      const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
      const fal = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
      return <a href={`#/model-version/${idOf(version)}`} key={idOf(version)}><strong>{str(version.name, "Open version")}</strong><small>{str(version.filename, "Checkpoint unavailable")} · Step {str(version.checkpoint_step, "not recorded")}</small><span><Status value={version.lifecycle_state} /><Status value={local.status ?? "unknown"} /><Status value={fal.status ?? "not registered"} /></span></a>;
    })}</div>
  </>;
}


function ModelTrainingRun({ run, modelId, track, initiallyExpanded }: { run: Row; modelId: string; track: string; initiallyExpanded: boolean }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const runId = idOf(run);
  const samples = useResource<unknown>(expanded ? `/api/runs/${runId}/samples` : null);
  const metrics = useResource<LossMetrics>(expanded ? `/api/runs/${runId}/metrics?name=${encodeURIComponent(track ? `loss.${track}` : "loss")}` : null);
  const config = useResource<Row>(expanded ? `/api/runs/${runId}/config` : null);
  const sampleRows = rows(samples.data).filter(sample => !track || str(sample.sample_track).toLowerCase() === track).slice(0, 9);
  return <details className="training-run model-training-run" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><strong>{str(run.name, "Training run")}</strong><span>{str(run.dataset_version_name, "No linked dataset")}</span><Status value={run.status} /></summary>
    <div className="training-run-body">
      <dl className="details"><dt>Base</dt><dd>{str(run.base_model)}</dd><dt>Checkpoints</dt><dd>{str(run.checkpoint_count, "0")}</dd><dt>Samples</dt><dd>{str(run.sample_count, "0")}</dd><dt>Dataset</dt><dd>{str(run.dataset_version_name, "Not linked")}</dd></dl>
      <div className="actions"><a className="button vela-button" href={`#/run/${runId}`}><ExternalLink size={14} />Open run and loss graph</a><a className="button vela-button" href={`#/samples/${runId}?return=${encodeURIComponent(`model/${modelId}`)}`}><FileImage size={14} />View all samples</a></div>
      {expanded && <>
        <div className="model-training-visuals">
          <section className="vela-section model-training-chart"><div className="vela-section-heading"><h3>{track ? `${track[0].toUpperCase()}${track.slice(1)} loss` : "Loss graph"}</h3></div><Notice error={metrics.error} loading={metrics.loading} /><LossGraph points={metrics.data?.points} /></section>
          <section className="vela-section model-training-samples"><div className="vela-section-heading"><h3>Sample images</h3></div><Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />{!!sampleRows.length && <div className="model-sample-grid">{sampleRows.map(sample => <figure key={idOf(sample)}><AssetImage assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id)} alt={str(sample.name, "Training sample")} /><figcaption>Step {str(sample.training_step, "—")}</figcaption></figure>)}</div>}</section>
        </div>
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
  return <div className="model-config-details"><dl className="details model-config-summary">{primary.map(([label, item]) => <Fragment key={label}><dt>{label}</dt><dd>{display(label, item)}</dd></Fragment>)}{other.map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{display(key, item)}</dd></Fragment>)}</dl>{value.training_config ? <section><h4>Nested training config</h4><Json value={value.training_config} /></section> : null}<section><h4>Imported source config</h4><Json value={raw} /></section></div>;
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
  return `#/gallery${query({ project: asset.project_id ?? metadata.project_id, dataset: asset.dataset_id ?? metadata.dataset_id, eval: asset.eval_run_id ?? metadata.eval_run_id, category: resolvedCategory, include_dataset_assets: resolvedCategory === "dataset_image" ? true : "", asset: asset.asset_revision_id ?? asset.asset_id ?? asset.id, return: returnTarget })}`;
}

export function isGalleryShortcutTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest("input, textarea, select, .vela-dropdown, [contenteditable=true]"));
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
        <Field label="Project"><Dropdown aria-label="Project" value={filters.project_id} onChange={value => setFilters({ ...filters, project_id: value, model_id: "", dataset_id: "" })} options={[{ value: "", label: "All projects" }, ...rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} /></Field>
        <Field label="Model"><Dropdown aria-label="Model" value={filters.model_id} onChange={value => setFilters({ ...filters, model_id: value })} options={[{ value: "", label: "All models" }, ...rows(models.data).map(model => ({ value: idOf(model), label: str(model.name) }))]} /></Field>
        <Field label="Dataset"><Dropdown aria-label="Dataset" value={filters.dataset_id} onChange={value => setFilters({ ...filters, dataset_id: value })} options={[{ value: "", label: "All datasets" }, ...rows(datasets.data).map(dataset => ({ value: idOf(dataset), label: str(dataset.name) }))]} /></Field>
        <Field label="Image set"><Dropdown aria-label="Image set" value={filters.category} onChange={value => setFilters({ ...filters, category: value, include_dataset_assets: value === "dataset_image" ? true : filters.include_dataset_assets })} options={[{ value: "", label: "All image sets" }, { value: "eval_output", label: "Eval outputs" }, { value: "sample", label: "Training samples" }, { value: "dataset_image", label: "Dataset images" }]} /></Field>
        <Field label="Search"><input value={filters.q} onChange={event => setFilters({ ...filters, q: event.target.value })} placeholder="Filename" /></Field>
        <Field label="Decision"><Dropdown aria-label="Decision" value={filters.decision} onChange={value => setFilters({ ...filters, decision: value })} options={[{ value: "", label: "Any" }, { value: "candidate", label: "Candidate" }, { value: "approved", label: "Approved" }, { value: "hold", label: "Hold" }, { value: "reject", label: "Reject" }]} /></Field>
        <Field label="Rating"><Dropdown aria-label="Rating" value={filters.rating} onChange={value => setFilters({ ...filters, rating: value })} options={[{ value: "", label: "Any" }, ...[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: `${value} star${value === 1 ? "" : "s"}` }))]} /></Field>
        <Field label="Sort"><Dropdown aria-label="Sort" value={filters.sort} onChange={value => setFilters({ ...filters, sort: value })} options={[{ value: "origin_time_desc", label: "Newest generated or modified" }, { value: "origin_time_asc", label: "Oldest generated or modified" }]} /></Field>
        <Field label="Dataset images"><label><input type="checkbox" checked={filters.include_dataset_assets} onChange={event => setFilters({ ...filters, include_dataset_assets: event.target.checked })} /> Include dataset members</label></Field>
        <Field label={`Thumbnail size · ${size}px`}><input aria-label="Thumbnail size" type="range" min="120" max="260" value={size} onChange={event => setSize(Number(event.target.value))} /></Field>
      </div>
      <div className="actions"><button onClick={() => setFilters(defaultFilters)} disabled={!isFiltered} title={!isFiltered ? "No additional filters are active" : undefined}><X size={14} />Clear filters</button><span className="vela-meta" aria-live="polite">{total} matching assets</span></div>
    </Panel>
    <Panel title="Assets" className="gallery-assets-panel">
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
  const closeButton = useRef<HTMLAnchorElement>(null);
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
  const relationships = metadata.relationships && typeof metadata.relationships === "object" ? metadata.relationships as Row : {};
  const relationName = (key: string, fallback = "Unknown") => {
    const ref = relationships[key] && typeof relationships[key] === "object" ? relationships[key] as Row : {};
    return str(ref.name, fallback);
  };
  const hrefFor = (item: Row) => galleryHash(filters, page, pageSize, str(item.asset_revision_id ?? item.asset_id ?? item.id), returnTarget);
  const rate = async (value: number) => { setRatingBusy(true); setRatingError(""); try { await api("/api/reviews", jsonBody({ subject_type: "asset", subject_id: id, rating: value, decision: "candidate" })); await reviews.reload(); } catch (reason) { setRatingError(errorText(reason)); } finally { setRatingBusy(false); } };
  useFocusWorkspace({
    active: true,
    containerRef: overlay,
    initialFocusRef: closeButton,
    onDismiss: () => {
      if (document.fullscreenElement) {
        void document.exitFullscreen();
        return;
      }
      location.hash = closeHref.slice(1);
    },
  });
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
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [id, previous && idOf(previous), next && idOf(next), ratingBusy, closeHref]);
  const originType = str(metadata.origin_type ?? (galleryContext.data?.current ? (galleryContext.data.current as Row).origin_type : undefined), "ASSET");
  const prompt = str(metadata.prompt ?? metadata.caption, "");
  const promptPresentation = captionPresentation({ caption: prompt, caption_format: metadata.caption_format }, str(asset.data?.name, "Selected image"));
  const replay = generationReplayFromMetadata({ ...metadata, project_id: metadata.project_id ?? asset.data?.project_id });
  return <div ref={overlay} className={`review-overlay vela-focus-workspace gallery-review-overlay ${fullscreen ? "is-fullscreen" : ""}`} role="dialog" aria-modal="true" aria-labelledby="image-review-title"><header className="viewer-topbar"><a href={closeHref}>Gallery</a><div className="viewer-actions"><button title={fullscreen ? "Exit full screen" : "Use full screen"} aria-label={fullscreen ? "Exit Full Screen" : "Full Screen"} onClick={() => { if (document.fullscreenElement) void document.exitFullscreen(); else void overlay.current?.requestFullscreen(); }}>{fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}<span>{fullscreen ? "Exit Full Screen" : "Full Screen"}</span></button><a ref={closeButton} className="button" aria-label="Close image review" href={closeHref}><X size={16} /> Close</a></div></header>
    <main className="image-review-stage"><div className="image-review-main"><nav className="image-nav" aria-label="Image navigation"><a aria-disabled={!previous} href={previous ? hrefFor(previous) : undefined}><ChevronLeft size={16} /> Previous</a><a aria-disabled={!next} href={next ? hrefFor(next) : undefined}>Next <ChevronRight size={16} /></a></nav><div className="image-review-media"><AssetImage className="fit" assetRevisionId={id} variant="content" alt={promptPresentation.alt} /></div>{prompt && (promptPresentation.format === "json" ? <pre className="image-review-prompt caption-json">{promptPresentation.rendered}</pre> : <p className="image-review-prompt">{prompt}</p>)}
      <div className="filmstrip">{navigationItems.slice(Math.max(0, index - 4), index + 5).map(item => <a className={idOf(item) === id ? "active" : ""} href={hrefFor(item)} key={idOf(item)}><AssetImage assetRevisionId={idOf(item)} alt={str(item.name)} /></a>)}</div></div>
      <aside id="storage_details" className="image-review-inspector"><h2 id="image-review-title">Image review <span className={`asset-origin-badge origin-${originType.toLowerCase()}`}>{originType}</span></h2><Notice error={asset.error || context.error || galleryContext.error} loading={asset.loading || context.loading || galleryContext.loading} /><dl className="details"><dt>File</dt><dd>{str(asset.data?.name)}</dd><dt>Type</dt><dd>{str(asset.data?.mime_type)}</dd><dt>Kind</dt><dd>{str(asset.data?.kind)}</dd><dt>Dimensions</dt><dd>{str(metadata.width ?? (context.data?.image as Row | undefined)?.width)} x {str(metadata.height ?? (context.data?.image as Row | undefined)?.height)}</dd><dt>Model</dt><dd>{relationName("model")}</dd><dt>Checkpoint</dt><dd>{relationName("checkpoint")}</dd><dt>Base model</dt><dd>{relationName("base_model")}</dd><dt>Training run</dt><dd>{relationName("training_run")}</dd><dt>Dataset</dt><dd>{relationName("dataset")}</dd><dt>Provider</dt><dd>{relationName("provider")}</dd></dl>
        <dl className="details image-generation-details"><dt>LoRA scale</dt><dd>{loraScaleFromMetadata(metadata) ?? "Not recorded"}</dd></dl>
        <details className="image-all-metadata"><summary>All image metadata</summary><Json value={{ asset: asset.data, image: context.data?.image, metadata, storage: context.data?.storage }} /></details>
        {replay && <a className="button vela-button" href={generatorHref("image", replay.context, id)}><ImagePlus size={15} /> Generate with these settings</a>}
        <div className="rating-control"><span>Image rating (keys 1–5)</span>{[1,2,3,4,5].map(value => <button className="vela-rating-button" aria-label={`${value} stars`} aria-pressed={currentRating === value} disabled={ratingBusy} key={value} onClick={() => void rate(value)}><Star size={16} fill={currentRating >= value ? "currentColor" : "none"} /></button>)}</div>{ratingError && <div className="vela-notice vela-notice-error" role="alert">{ratingError}</div>}
        <div className="image-delete-control"><button className="vela-button vela-button-danger" disabled={deleteBusy} onClick={async () => { if (!confirm(`Delete ${str(asset.data?.name, "this image")}? This removes DAM metadata and supported local content.`)) return; setDeleteBusy(true); setDeleteError(""); try { const result = await api<Row>(`/api/assets/${id}?report=true&delete_content=true`, { method: "DELETE" }); const retained = rows(result.storage).filter(item => item.status !== "deleted"); onDeleteNotice(retained.length ? `${retained.length} storage object(s) retained; DAM metadata deleted.` : "Image and local content deleted."); const destination = next ? hrefFor(next) : previous ? hrefFor(previous) : closeHref; await onDeleted(); location.hash = destination.slice(1); } catch (reason) { setDeleteError(errorText(reason)); } finally { setDeleteBusy(false); } }}><Trash2 size={15} />{deleteBusy ? "Deleting…" : "Delete image"}</button>{deleteError && <div className="vela-notice vela-notice-error" role="alert">{deleteError}</div>}</div>
        <section><h3>Comments</h3><div className="comments">{rows(comments.data).map(comment => <article key={idOf(comment)}><strong>{str(comment.profile_name, "Operator")}</strong><p>{str(comment.body)}</p></article>)}</div><Form submit="Add comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: "asset", subject_id: id, body: form.get("body") })); await comments.reload(); }}><Field label="Comment"><textarea name="body" required /></Field></Form></section>
      </aside></main>
  </div>;
}

export function galleryPreviewGeometry(containerWidth: number, preferredSize: number) {
  const width = Math.max(0, Math.min(containerWidth, preferredSize));
  return { width, height: width, reflows: width < preferredSize, overflows: width > containerWidth };
}

export function AssetGrid({ assets, hrefForAsset }: { assets: Row[]; hrefForAsset?: (asset: Row) => string }) {
  return <div className="asset-grid">{assets.map((asset, index) => {
    const metadata = asset.metadata && typeof asset.metadata === "object" ? asset.metadata as Row : {};
    const relationships = metadata.relationships && typeof metadata.relationships === "object" ? metadata.relationships as Row : {};
    const refName = (key: string) => {
      const ref = relationships[key] && typeof relationships[key] === "object" ? relationships[key] as Row : {};
      return str(ref.name, "");
    };
    const originType = str(metadata.origin_type ?? asset.origin_type, "ASSET").toUpperCase();
    const baseModel = refName("base_model");
    const model = refName("model");
    const checkpoint = refName("checkpoint");
    const originLine = [baseModel, originType].filter(Boolean).join(" · ");
    const modelLine = [model, checkpoint].filter(Boolean).join(" · ");
    const assetId = String(asset.asset_revision_id ?? asset.asset_id ?? asset.id);
    const name = str(asset.filename ?? asset.name, "Generated image");
    const replay = generationReplayFromMetadata({ ...metadata, project_id: metadata.project_id ?? asset.project_id });
    const loraScale = loraScaleFromMetadata(metadata);
    return <article className="asset-card" key={idOf(asset) || index}>
      <a className="asset-card-preview" href={hrefForAsset ? hrefForAsset(asset) : galleryOpenHref(asset)}><AssetImage assetRevisionId={assetId} alt={name} loading="lazy" /><span className="asset-card-name">{name}</span></a>
      <small className="asset-card-footer"><span>{originLine || "Unknown"}</span><span>{modelLine || "Unknown"}</span><span>LoRA scale · {loraScale ?? "Not recorded"}</span></small>
      {replay && <a className="button vela-button asset-card-generate" href={generatorHref("image", replay.context, assetId)}><ImagePlus size={13} /> Generate</a>}
    </article>;
  })}</div>;
}
