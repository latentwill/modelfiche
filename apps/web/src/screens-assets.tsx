import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { readPreference, writePreference } from "./preferences";
import { canonicalWorkspaceHref } from "./workspace-routing";
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
import { api, idOf, jsonBody, listOf, patchBody, query, routeQuery, str } from "./api";
import { galleryOpenHref, safeReturnTarget } from "./gallery-routing";
import { AssetImage } from "./asset-image";
import { useResource, useRunLive } from "./hooks";
import { CopyMetadataButton, DataTable, Dropdown, Field, Form, Json, Notice, Page, Pagination, Panel, SelectRecords, Status, useDragTextSelection, useFocusWorkspace } from "./ui";
import { generationReplayFromMetadata, generatorHref, loraScaleFromMetadata, normalizedParametersFromMetadata } from "./generation";
import { DitherLossChart } from "./dither-loss-chart";
import { LineageList } from "./lineage";
import { DitherStackedBar, type DitherBarSegment } from "./dither-stacked-bar";
import type { DitherColor } from "./components/dither-kit/palette";

type Row = Record<string, unknown>;
const TERMINAL_RUN_STATUS_OPTIONS = [
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "interrupted", label: "Interrupted" },
  { value: "canceled", label: "Canceled" },
];
const TERMINAL_RUN_STATUSES: Record<string, true> = {
  completed: true,
  failed: true,
  interrupted: true,
  canceled: true,
};
const rows = (value: unknown) => listOf<Row>(value);
function primarySubdataset(item: Row): Row | undefined {
  const subsets = rows(item.subdatasets);
  return subsets.find(subset => str(subset.membership_role) === "primary") ?? subsets[0];
}

function sortDatasetItemsBySubdataset(items: Row[]): Row[] {
  return [...items].sort((left, right) => {
    const leftSubset = primarySubdataset(left);
    const rightSubset = primarySubdataset(right);
    return Number(leftSubset?.position ?? Number.MAX_SAFE_INTEGER) - Number(rightSubset?.position ?? Number.MAX_SAFE_INTEGER)
      || str(leftSubset?.name).localeCompare(str(rightSubset?.name))
      || Number(left.position ?? 0) - Number(right.position ?? 0);
  });
}

function isInSubdataset(item: Row, subsetId: string): boolean {
  return !subsetId || rows(item.subdatasets).some(subset => idOf(subset) === subsetId);
}

function isGridOutput(metadata: Row, asset: Row = {}) {
  const grid = metadata.grid && typeof metadata.grid === "object" ? metadata.grid as Row : {};
  return [metadata.grid_cell_id, metadata.grid_definition_id, metadata.grid_id, grid.cell_id, grid.definition_id, asset.grid_cell_id, asset.grid_definition_id, asset.grid_id]
    .some(value => Boolean(String(value ?? "").trim()));
}

function originDisplayLabel(originType: string, metadata: Row, asset?: Row) {
  if (originType.toUpperCase() !== "EVAL") return originType;
  return isGridOutput(metadata, asset) ? "GRID" : "IMAGE";
}

function workflowReference(metadata: Row) {
  const candidate = metadata.workflow && typeof metadata.workflow === "object" ? metadata.workflow as Row : {};
  const rawUrl = str(candidate.url ?? metadata.workflow_url, "");
  const assetId = str(candidate.asset_id, "");
  const name = str(candidate.name ?? candidate.asset_name ?? metadata.workflow_name, "");
  if (!rawUrl && !name && !assetId) return null;
  return {
    name: name || "Workflow asset",
    href: /^https?:\/\//i.test(rawUrl) ? rawUrl : "",
  };
}
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const TERMINAL_JOB_STATES: Record<string, true> = { succeeded: true, failed: true, canceled: true };
async function waitForJob(jobId: string) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const job = await api<Row>(`/api/jobs/${encodeURIComponent(jobId)}`);
    const state = str(job.state, "").toLowerCase();
    if (state === "succeeded") return;
    if (TERMINAL_JOB_STATES[state]) throw new Error(`Run refresh ${state}: ${str(job.error, "the worker did not complete")}`);
    await new Promise<void>(resolve => window.setTimeout(resolve, 1000));
  }
  throw new Error("Run refresh is still running; reload this page when it completes.");
}
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
      <AssetImage assetRevisionId={str(asset.asset_revision_id ?? asset.asset_id ?? asset.id)} alt={caption.alt} loading="lazy" maxPixels={256} />
      {showTrainingDetails && (caption.raw || step || uploadState) && <span className="training-sample-details">
        {caption.raw && <strong>{caption.alt}</strong>}
        <small>{[step && `Step ${step}`, uploadState && `Upload ${uploadState}`].filter(Boolean).join(" · ")}</small>
      </span>}
    </a>;
  })}</div>;
}

export function DatasetsScreen({ projectId = "", params = new URLSearchParams() }: { projectId?: string; params?: URLSearchParams }) {
  const [page, setPage] = useState(() => Math.max(1, Number(params.get("page")) || 1));
  const resource = useResource<Row>(`/api/datasets${query({ project_id: projectId, paginated: true, limit: 50, offset: (page - 1) * 50 })}`);
  const data = rows(resource.data);
  const total = Number(resource.data?.total ?? data.length);
  return <Page
    title="Datasets"
    subtitle="Versioned training images and captions"
    actions={<a className="button vela-button vela-button-primary" href={`#/transfers${routeQuery({ mode: "import", project: projectId })}`}><FolderInput size={15} />Import dataset</a>}
  >
    <Panel title="Dataset registry">
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />
      {!!data.length && <DataTable rows={data} total={total} page={page} pageSize={50} onPageChange={setPage} columns={[["name", "Name"], ["project_name", "Project"], ["current_version", "Version"], ["item_count", "Items"], ["updated_at", "Updated"]]} onRow={row => { location.hash = `dataset/${idOf(row)}${routeQuery({ project: projectId })}`; }} />}
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
  const [subsetFilter, setSubsetFilter] = useState("");
  const [itemSort, setItemSort] = useState<"position" | "subdataset">("position");
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
  const publishedItems = useResource<Row>(activeVersionId && !activeDraft ? `/api/dataset-versions/${activeVersionId}/items${query({ subset_id: subsetFilter || undefined, sort: itemSort, limit: pageSize, offset: (page - 1) * pageSize, paginated: true })}` : null);
  const draftItemsResource = useResource<Row>(activeDraft?.id ? `/api/dataset-drafts/${activeDraft.id}/items${query({ subset_id: subsetFilter || undefined, sort: itemSort, limit: pageSize, offset: (page - 1) * pageSize, paginated: true })}` : null);
  const compositionResource = useResource<unknown>(activeVersionId && !activeDraft ? `/api/dataset-versions/${activeVersionId}/subsets` : null);
  const compositionSubsets = activeDraft ? rows(activeDraft.subsets) : rows(compositionResource.data);
  const rawItems = activeDraft ? rows(draftItemsResource.data?.items) : rows(publishedItems.data?.items);
  const items = rawItems;
  const allDraftItems = activeDraft ? rows(activeDraft.items) : [];
  const batchItems = allDraftItems.length >= Number(draftItemsResource.data?.total ?? 0) ? allDraftItems : items;
  const totalItems = activeDraft ? Number(draftItemsResource.data?.total ?? allDraftItems.length ?? items.length) : Number(publishedItems.data?.total ?? activeVersion?.item_count ?? 0);
  const visibleItems = items;
  const visibleItemIds = visibleItems.map(item => idOf(item)).filter(Boolean);
  const batchTargetIds = batchScope === "all" ? [] : batchScope === "visible" ? visibleItemIds : selected;
  const batchTargetCount = batchScope === "all" ? totalItems : batchTargetIds.length;
  const firstVisible = visibleItems.length ? (page - 1) * pageSize + 1 : 0;
  const lastVisible = Math.min(page * pageSize, totalItems);
  const draftFingerprint = batchItems.map(item => [
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
    item_ids: batchScope === "all" ? batchItems.map(item => idOf(item)) : batchTargetIds,
    prompt: captionPrompt.trim(),
    provider: captionProvider,
    model: captionModel.trim(),
    caption_format: captionFormat,
  });

  useEffect(() => setPage(1), [activeVersionId, pageSize, subsetFilter, itemSort]);
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
      await Promise.all([draftItemsResource.reload(), operationHistory.reload()]);
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
      await Promise.all([draftItemsResource.reload(), operationHistory.reload()]);
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
    const targetItems = (batchScope === "all" ? batchItems : items).filter(item => {
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
        if (generated.length) {
          const generatedById = new Map(generated.map(item => [idOf(item), item]));
          draftItemsResource.setData(previous => previous ? { ...previous, items: rows(previous.items).map(item => generatedById.get(idOf(item)) ? { ...item, ...generatedById.get(idOf(item)) } : item) } : previous);
          setDraft(previous => {
            if (!previous) return previous;
            return { ...previous, items: rows(previous.items).map(item => generatedById.get(idOf(item)) ? { ...item, ...generatedById.get(idOf(item)) } : item) };
          });
        }
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
      await Promise.all([draftItemsResource.reload(), operationHistory.reload()]);
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

  const compositionColors: DitherColor[] = ["blue", "cyan", "teal", "green", "mint", "grey"];
  const compositionSegments: DitherBarSegment[] = compositionSubsets.map((subset, index) => ({
    key: idOf(subset) || str(subset.key),
    label: str(subset.name, str(subset.key, "Sub-dataset")),
    value: Number(subset.item_count ?? 0),
    color: compositionColors[index % compositionColors.length],
  }));

  async function refreshDraftComposition() {
    if (!draft?.id) return;
    const refreshed = await api<Row>(`/api/dataset-drafts/${draft.id}`);
    setDraft(refreshed);
    await draftItemsResource.reload();
  }

  async function updateSubset(subsetId: string, changes: Row) {
    if (!draft?.id) return;
    setBusy(`subset-${subsetId}`); setActionError("");
    try {
      await api(`/api/dataset-drafts/${draft.id}/subsets/${subsetId}`, patchBody(changes));
      await refreshDraftComposition();
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function removeSubset(subsetId: string) {
    if (!draft?.id || !window.confirm("Remove this sub-dataset and its draft memberships?")) return;
    setBusy(`subset-${subsetId}`); setActionError("");
    try {
      await api(`/api/dataset-drafts/${draft.id}/subsets/${subsetId}`, { method: "DELETE" });
      await refreshDraftComposition();
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function moveSubset(index: number, offset: number) {
    const target = compositionSubsets[index + offset];
    const current = compositionSubsets[index];
    if (!draft?.id || !target || !current) return;
    setBusy(`subset-${idOf(current)}`); setActionError("");
    try {
      await Promise.all([
        api(`/api/dataset-drafts/${draft.id}/subsets/${idOf(current)}`, patchBody({ position: Number(target.position ?? index + offset) })),
        api(`/api/dataset-drafts/${draft.id}/subsets/${idOf(target)}`, patchBody({ position: Number(current.position ?? index) })),
      ]);
      await refreshDraftComposition();
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function assignSubset(subsetId: string) {
    if (!draft?.id || !selected.length) return;
    setBusy(`assign-${subsetId}`); setActionError("");
    try {
      await api(`/api/dataset-drafts/${draft.id}/subset-assignments`, jsonBody({ subset_id: subsetId, draft_item_ids: selected, membership_role: "primary" }));
      await refreshDraftComposition();
      setMessage(`Assigned ${selected.length} selected image${selected.length === 1 ? "" : "s"} to a sub-dataset.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }
  const subtitle = `${str(data?.project_name, "Project dataset")} · ${totalItems} images · ${activeDraft ? "Working draft" : `v${str(activeVersion?.version_number ?? data?.current_version, "-")}`}`;
  return <Page title={str(data?.name, "Dataset")} subtitle={subtitle} actions={<>
    {activeVersionId && !activeDraft ? <a className="button vela-button" href={`#/transfers${routeQuery({ mode: "export", dataset_version: activeVersionId, project: data?.project_id })}`}><Download size={15} />Export version</a> : <button disabled title="Select a published dataset version before exporting"><Download size={15} />Export unavailable</button>}
    {!activeDraft && activeVersion?.status === "published" && <a className="button vela-button vela-button-primary" href={`#/training-new${routeQuery({ dataset: id, version: activeVersionId, project: data?.project_id })}`}><Sparkles size={15} />Start training</a>}
    {!activeDraft && !draft && <button className="vela-button" onClick={() => void createDraft()} disabled={!activeVersionId || busy === "draft"} title={!activeVersionId ? "Import a dataset version before editing captions" : undefined}><Plus size={15} />{busy === "draft" ? "Creating..." : "Clone as new version"}</button>}
    {!activeDraft && draft && <button className="vela-button" onClick={() => { setSelectedVersionId(str(draft.base_version_id)); setViewingDraft(true); setSelected([]); setPreview(null); }}><Plus size={15} />Return to working draft</button>}
  </>}>
    <Notice error={resource.error || versionsResource.error} loading={resource.loading} />
    <ActionMessage message={message} error={actionError} />
    <ObjectImageStrip assets={activeDraft ? allDraftItems : items} label="Dataset image preview" datasetId={id} projectId={str(data?.project_id)} />

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

    <Panel title="Dataset composition">
      <Notice error={compositionResource.error} loading={!activeDraft && compositionResource.loading} empty={!compositionResource.loading && !compositionSubsets.length} emptyText="No sub-datasets in this version" emptyHint={activeDraft ? "Create a named sub-dataset before publishing this composed version." : "Clone this version to define composition groups."} />
      {!!compositionSubsets.length && <div className="composition-grid">
        <div>
          <DitherStackedBar segments={compositionSegments} label={`${compositionSubsets.length} sub-datasets containing ${compositionSegments.reduce((sum, segment) => sum + segment.value, 0)} image memberships`} />
          <table className="composition-table" aria-label="Dataset composition values">
            <thead><tr><th>Sub-dataset</th><th>Role</th><th>Images</th><th>Share</th></tr></thead>
            <tbody>{compositionSubsets.map((subset, index) => {
              const count = Number(subset.item_count ?? 0);
              const total = compositionSegments.reduce((sum, segment) => sum + segment.value, 0);
              return <tr key={idOf(subset) || index}><td>{str(subset.name, str(subset.key))}</td><td>{str(subset.role, "style")}</td><td>{count}</td><td>{total ? `${(count / total * 100).toFixed(1)}%` : "0%"}</td></tr>;
            })}</tbody>
          </table>
        </div>
        <dl className="details">
          <dt>Total included images</dt><dd>{totalItems}</dd>
          <dt>Sub-datasets</dt><dd>{compositionSubsets.length}</dd>
          <dt>Shared trigger words</dt><dd>{rows(activeVersion?.trigger_words ?? data?.trigger_words).map(value => str(value)).filter(Boolean).join(", ") || "None"}</dd>
          <dt>Composition digest</dt><dd><code>{str(activeVersion?.composition_digest ?? activeVersion?.content_digest, "Draft")}</code></dd>
        </dl>
      </div>}
      {activeDraft && <div className="composition-editor">
        <h3>Edit sub-datasets</h3>
        <Form submit="Create sub-dataset" onSubmit={async form => {
          if (!draft?.id) return;
          await api(`/api/dataset-drafts/${draft.id}/subsets`, jsonBody({
            key: str(form.get("key")), name: str(form.get("name")), role: str(form.get("role"), "style"),
            description: str(form.get("description")) || null, color_token: str(form.get("color_token")) || null,
          }));
          await refreshDraftComposition();
        }}>
          <div className="form-grid">
            <Field label="Key" hint="Stable machine-readable identity."><input name="key" required pattern="[a-z0-9]+(?:-[a-z0-9]+)*" placeholder="core-gestures" /></Field>
            <Field label="Name"><input name="name" required placeholder="Core gestures" /></Field>
            <Field label="Role"><Dropdown name="role" defaultValue="style" options={[{ value: "style", label: "Style" }, { value: "subject", label: "Subject" }, { value: "source", label: "Source" }, { value: "quality", label: "Quality" }, { value: "holdout", label: "Holdout" }, { value: "custom", label: "Custom" }]} /></Field>
            <Field label="Description"><input name="description" /></Field>
          </div>
        </Form>
        {!!compositionSubsets.length && <div className="caption-versions" aria-label="Editable sub-datasets">{compositionSubsets.map((subset, index) => <div className="caption-version" key={idOf(subset)}>
          <span className="caption-version-number">{Number(subset.item_count ?? 0)}</span>
          <span className="caption-version-body"><strong>{str(subset.name)}</strong><small>{str(subset.key)} · {str(subset.role, "style")}</small></span>
          <span className="actions">
            <button type="button" onClick={() => void assignSubset(idOf(subset))} disabled={!selected.length || Boolean(busy)} title={selected.length ? `Assign ${selected.length} selected images as primary members` : "Select images below first"}>Assign {selected.length || ""}</button>
            <button type="button" onClick={() => void moveSubset(index, -1)} disabled={!index || Boolean(busy)} aria-label={`Move ${str(subset.name)} up`}>↑</button>
            <button type="button" onClick={() => void moveSubset(index, 1)} disabled={index === compositionSubsets.length - 1 || Boolean(busy)} aria-label={`Move ${str(subset.name)} down`}>↓</button>
            <button type="button" onClick={() => { const name = window.prompt("Sub-dataset name", str(subset.name)); if (name?.trim()) void updateSubset(idOf(subset), { name: name.trim() }); }} disabled={Boolean(busy)}>Rename</button>
            <button type="button" onClick={() => void removeSubset(idOf(subset))} disabled={Boolean(busy)}>Remove</button>
          </span>
        </div>)}</div>}
      </div>}
    </Panel>
    {rows(data?.training_runs).length > 0 && <Panel title="Training runs using this dataset"><DataTable rows={rows(data?.training_runs)} columns={[["name", "Run"], ["status", "Status"], ["dataset_version_id", "Dataset version"]]} onRow={row => { location.hash = `run/${idOf(row)}${routeQuery({ project: data?.project_id })}`; }} /></Panel>}

    {activeDraft && <Panel title="Batch caption operations">
      <div className="selection-toolbar">
        <div>
          <strong>{selected.length} selected · showing {firstVisible}–{lastVisible} of {totalItems}</strong>
          <span className="vela-meta" aria-live="polite">Batch scope: {batchScope === "visible" ? `${visibleItemIds.length} visible items` : batchScope === "selected" ? `${selected.length} selected items` : `all ${totalItems} draft items`}.</span>
        </div>
        <div className="actions">
          <button onClick={() => setSelected(previous => [...new Set([...previous, ...visibleItemIds])])} disabled={!visibleItemIds.length || visibleItemIds.every(itemId => selected.includes(itemId))}><Check size={14} />Select visible {visibleItemIds.length}</button>
          <button onClick={() => setSelected(batchItems.map(item => idOf(item)).filter(Boolean))} disabled={!batchItems.length || selected.length === batchItems.length}><Check size={14} />Select all {totalItems}</button>
          <button onClick={() => setSelected([])} disabled={!selected.length}><X size={14} />Clear selection</button>
          <button onClick={() => void discardDraft()} disabled={busy === "discard" || captionBusy}><Trash2 size={14} />{busy === "discard" ? "Discarding..." : "Discard draft"}</button>
        </div>
      </div>
      <Field label="Batch scope" hint="Visible affects only this page; Selected follows checked images across pages; Entire draft includes every image.">
        <Dropdown aria-label="Batch scope" value={batchScope} onChange={value => setBatchScope(value as BatchScope)} options={[
          { value: "visible", label: `Visible page (${visibleItemIds.length})` },
          { value: "selected", label: `Selected (${selected.length})`, disabled: !selected.length },
          { value: "all", label: `Entire draft (${totalItems})` },
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
        <div className="actions caption-catalog-actions"><button type="button" onClick={() => void captionCatalog.reload()} disabled={captionCatalog.loading}><RefreshCw size={14} />{captionCatalog.loading ? "Loading models…" : "Reload models"}</button><a className="button" href={`#/settings${routeQuery({})}`}><Settings2 size={14} />Provider settings</a></div>
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
      <Notice error={activeDraft ? draftItemsResource.error : publishedItems.error} loading={activeDraft ? draftItemsResource.loading : publishedItems.loading} empty={!(activeDraft ? draftItemsResource.loading : publishedItems.loading) && !totalItems} />
      {!!compositionSubsets.length && <div className="dataset-item-controls">
        <Field label="Sub-dataset"><Dropdown aria-label="Filter dataset images by sub-dataset" value={subsetFilter} onChange={setSubsetFilter} options={[
          { value: "", label: "All sub-datasets" },
          ...compositionSubsets.map(subset => ({ value: idOf(subset), label: `${str(subset.name, str(subset.key, "Sub-dataset"))} · ${Number(subset.item_count ?? 0)} images` })),
        ]} /></Field>
        <Field label="Order"><Dropdown aria-label="Sort dataset images" value={itemSort} onChange={value => setItemSort(value === "subdataset" ? "subdataset" : "position")} options={[
          { value: "position", label: "Dataset order" },
          { value: "subdataset", label: "Sub-dataset, then dataset order" },
        ]} /></Field>
      </div>}
      {!(activeDraft ? draftItemsResource.loading : publishedItems.loading) && !totalItems && Boolean(data?.empty_reason) && <div className="vela-notice" role="status"><strong>Dataset is empty</strong><span>{str(data?.empty_reason)}</span></div>}
      {!!visibleItems.length && <div className="caption-list">{visibleItems.map((item, index) => <CaptionItem key={idOf(item) || index} item={item} selected={selected.includes(idOf(item))} onSelect={checked => setSelected(previous => {
        const itemId = idOf(item);
        return checked ? [...new Set([...previous, itemId])] : previous.filter(value => value !== itemId);
      })} datasetId={id} projectId={str(data?.project_id)} draftId={str(activeDraft?.id, "")} onSaved={created => {
        if (!activeDraft) return;
        draftItemsResource.setData(previous => previous ? { ...previous, items: rows(previous.items).map(candidate => idOf(candidate) === idOf(item) ? { ...candidate, ...created } : candidate) } : previous);
        void operationHistory.reload();
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
  const imageHref = galleryOpenHref({ ...item, dataset_id: datasetId, project_id: projectId }, "dataset_image");
  const subdatasets = rows(item.subdatasets);
  const subdatasetLabel = subdatasets.map(subset => str(subset.name, str(subset.key, "Sub-dataset"))).join(", ");
  useEffect(() => setCaption(captionPresentation(item, filename).rendered), [item.caption, item.caption_format, filename]);
  return <article className={`caption-item ${selected ? "selected" : ""}`}>
    <div className="caption-image">
      <a href={imageHref} aria-label={`Open ${filename} in the image reviewer`} title={`Open ${filename} in the image reviewer`}><AssetImage assetRevisionId={String(assetId)} alt={presentation.alt} loading="lazy" maxPixels={256} /></a>
    </div>
    <div className="caption-copy">
      <div className="caption-heading"><strong>{filename}</strong><span className="caption-heading-actions">{draftId && <label className="caption-select"><input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} aria-label={`Select ${filename}`} /> Select</label>}<a className="caption-open-link" href={imageHref} aria-label={`Open ${filename} in the image reviewer`}>Open image</a><Status value={presentation.format.toUpperCase()} /></span></div>
      {!!subdatasetLabel && <dl className="caption-subdataset"><dt>Sub-dataset</dt><dd>{subdatasetLabel}</dd></dl>}
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
export function RunsScreen({ projectId = "", params = new URLSearchParams() }: { projectId?: string; params?: URLSearchParams }) {
  const [page, setPage] = useState(() => Math.max(1, Number(params.get("page")) || 1));
  const resource = useResource<Row>(`/api/runs${query({ project_id: projectId, paginated: true, limit: 50, offset: (page - 1) * 50 })}`);
  const data = rows(resource.data);
  const total = Number(resource.data?.total ?? data.length);
  return <Page title="Training runs" subtitle="Imported trainer output, checkpoints, samples, and normalized configuration">
    <Panel title="Run registry">
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />
      {!!data.length && <DataTable rows={data} total={total} page={page} pageSize={50} onPageChange={setPage} columns={[["name", "Run"], ["project_name", "Project"], ["trainer", "Trainer"], ["base_model", "Base model"], ["status", "Status"], ["checkpoint_count", "Checkpoints"]]} onRow={row => { location.hash = `run/${idOf(row)}${routeQuery({ project: projectId })}`; }} />}
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
  const isMerge = str(runView.run_kind) === "checkpoint_merge";
  const mergeSummary = (runView.merge_summary ?? {}) as Row;
  const latestLoss = runView.latest_loss && typeof runView.latest_loss === "object" && !Array.isArray(runView.latest_loss) ? runView.latest_loss as Row : null;
  const lossMetricName = str(latestLoss?.name, "loss");
  const metrics = useResource<LossMetrics>(live.data ? `/api/runs/${id}/metrics${query({ name: lossMetricName })}` : null);
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
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [terminalStatus, setTerminalStatus] = useState("completed");
  const sampleRows = rows(samples.data);
  const sampleSteps = [...new Set(sampleRows.map(sample => str(sample.training_step, "unassigned")))];
  const datasetVersionId = str(runView.dataset_version_id, "");
  const runProjectId = str(runView.project_id, "");
  const linkedDatasetId = str(runView.dataset_id, "");
  const uploadSummary = summarizeLiveUploads(runView.uploads, runView.upload_status);
  const lossRevision = `${lossMetricName}:${str(latestLoss?.step, "")}:${str(latestLoss?.value ?? latestLoss?.value_text, "")}`;
  const sampleRevision = `${str(runView.sample_count, "")}:${rows(runView.uploads).map(upload => str(upload.status ?? upload.state ?? upload.upload_status)).join(",")}`;
  const isTerminal = Boolean(TERMINAL_RUN_STATUSES[str(runView.status).toLowerCase()]);
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
      const jobId = str(job.job_id, "");
      if (jobId) await waitForJob(jobId);
      await Promise.all([run.reload(), samples.reload(), checkpointsResource.reload(), metrics.reload(), learningRateMetrics.reload()]);
      setMessage(`Run refresh completed${jobId ? ` as job ${jobId}` : ""}. Local image copies are ready.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function terminalizeRun() {
    setBusy("terminal-status"); setMessage(""); setActionError("");
    try {
      await api(`/api/runs/${id}/status`, patchBody({ status: terminalStatus }));
      await run.reload();
      setMessage(`Run status set to ${terminalStatus}.`);
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }

  async function archiveRun() {
    setBusy("archive"); setMessage(""); setActionError("");
    try {
      await api(`/api/runs/${id}/archive`, jsonBody({}));
      await run.reload();
      setMessage("Run archived.");
    } catch (reason) { setActionError(errorText(reason)); } finally { setBusy(""); }
  }


  const lastEventDate = new Date(str(runView.last_event_at, ""));
  const lastEventText = Number.isNaN(lastEventDate.valueOf()) ? "Unavailable" : lastEventDate.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
  const latestLossValue = str(latestLoss?.value_text ?? latestLoss?.value ?? metrics.data?.final_value, "Unavailable");
  if (!run.data && run.loading) return <Page title="Training run" subtitle="Loading imported run details"><section aria-busy="true"><Notice loading /></section></Page>;
  if (!run.data && run.error) return <Page title="Run unavailable" subtitle={`Run ${id}`}><Notice error={run.error} /><div className="actions"><a className="button vela-button" href={`#/runs${routeQuery({ project: runView.project_id })}`}>Back to runs</a><button onClick={() => void run.reload()}>Retry</button></div></Page>;
  return <Page title={str(runView.name, isMerge ? "Checkpoint merge" : "Training run")} subtitle={isMerge ? `${str(runView.base_model, "Unknown base")} · ${str(mergeSummary.notation, str(mergeSummary.operator, "Merge recipe"))}` : `${str(runView.base_model, "Unknown base")} · ${str(runView.dataset_version_name, "No dataset")} · ${str(runView.trainer, "Unknown trainer")}`} actions={<>
    {!isMerge && (datasetId ? <a className="button vela-button" href={`#/dataset/${datasetId}${routeQuery({ project: runView.project_id })}`}><Database size={15} />Dataset</a> : <button disabled title={datasetError || (datasetLoading ? "Resolving the linked dataset version" : "This run does not reference an available dataset version")}><Database size={15} />{datasetLoading ? "Finding dataset..." : "Dataset unavailable"}</button>)}
    {!isMerge && <a className="button vela-button" href={`#/samples/${id}${routeQuery({ step: undefined, return: `run/${id}`, project: runView.project_id })}`}><FileImage size={15} />Samples</a>}
    {runView.project_id ? <a className="button vela-button" href={`#/transfers${routeQuery({ mode: "export", project: runView.project_id })}`}><Download size={15} />Export project</a> : <button disabled title="This run is not attached to an exportable project"><Download size={15} />Export unavailable</button>}
    {!isMerge && <button className="vela-button vela-button-primary" onClick={() => void refreshRun()} disabled={busy === "refresh"}><RefreshCw size={15} />{busy === "refresh" ? "Refreshing..." : "Refresh import"}</button>}
    {!isMerge && !runView.archived_at && !isTerminal && <><Dropdown aria-label="Terminal status" value={terminalStatus} onChange={setTerminalStatus} options={TERMINAL_RUN_STATUS_OPTIONS} /><button className="vela-button" onClick={() => void terminalizeRun()} disabled={busy === "terminal-status"}>{busy === "terminal-status" ? "Saving..." : "Set terminal status"}</button></>}
    {!isMerge && !runView.archived_at && isTerminal && <button className="vela-button" onClick={() => void archiveRun()} disabled={busy === "archive"}><Archive size={15} />{busy === "archive" ? "Archiving..." : "Archive run"}</button>}
  </>}>
    <ActionMessage message={message} error={actionError} />
    {!isMerge && <ObjectImageStrip assets={sampleRows.slice().reverse()} label="Latest training samples" showTrainingDetails />}
    {live.error && <div className="vela-notice" role="status" aria-live="polite"><strong>{LIVE_CONNECTION_LABELS[live.connection]}</strong><span>{live.error}</span></div>}
    {(run.data || live.data) && (isMerge ? <div className="run-summary live-run-summary vela-context-stats" aria-label="Checkpoint merge status">
      <div className="vela-stat"><span>Status</span><Status value={runView.status} /></div>
      <div className="vela-stat"><span>Operator</span><strong>{str(mergeSummary.operator).replaceAll("_", " ")}</strong></div>
      <div className="vela-stat"><span>Inputs</span><strong>{rows(mergeSummary.inputs).length}</strong></div>
      <div className="vela-stat"><span>Output rank</span><strong>{str((mergeSummary.output as Row | undefined)?.rank, "—")}</strong></div>
      <div className="vela-stat"><span>Phase</span><strong>{str(mergeSummary.last_phase, str(runView.status))}</strong></div>
      <div className="vela-stat"><span>Recipe</span><strong>{str(mergeSummary.recipe_digest).slice(0, 12)}</strong></div>
    </div> : <div className="run-summary live-run-summary vela-context-stats" aria-label="Live training status">
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
    </div>)}
    {runView.source_status === "failed" && runView.usable_output === true && <div className="vela-notice" role="status"><strong>Usable training output recovered</strong><span>The trainer process reported “{str(runView.source_message, "failed termination")}”, but the DAM found {checkpoints.length} usable checkpoints and retained the original termination details in normalized configuration.</span></div>}

    {isMerge && <Panel title="Checkpoint merge flow"><MergeFlowChart summary={mergeSummary} /></Panel>}
    {!isMerge && <Panel title="Training metrics">
      <Notice error={metrics.error || learningRateMetrics.error} loading={metrics.loading || learningRateMetrics.loading} />
      <div className="training-metric-summary"><div><span>{lossMetricName || "Latest loss"}</span><strong>{latestLossValue}</strong></div><div><span>Configured learning rate</span><strong>{scientificNotation(runView.learning_rate ?? (runView.normalized_config as Row | undefined)?.learning_rate)}</strong></div><div><span>Loss points</span><strong>{normalizeLossPoints(metrics.data?.points).length}</strong></div><div><span>LR points</span><strong>{normalizeLossPoints(learningRateMetrics.data?.points).length}</strong></div></div>
      <div className="training-chart-grid"><section><header><strong>{lossMetricName || "Training loss"}</strong><small>Lower is generally better</small></header><LossGraph points={metrics.data?.points} /></section><section><header><strong>Learning rate</strong><small>Optimizer schedule</small></header>{normalizeLossPoints(learningRateMetrics.data?.points).length ? <FlintMetricChart points={normalizeLossPoints(learningRateMetrics.data?.points)} label="Learning rate" /> : <div className="loss-graph-empty" role="status"><strong>No learning-rate series available</strong><small>The configured value remains visible above.</small></div>}</section></div>
    </Panel>}

    <Panel title="Run checkpoint artifacts">
      <Notice error={!enrichedCheckpoints.length ? checkpointsResource.error : undefined} loading={checkpointsResource.loading && !enrichedCheckpoints.length} empty={!checkpointsResource.loading && !checkpoints.length} emptyText="No checkpoint artifacts" emptyHint="No checkpoint files were indexed for this run." />
      {!!checkpoints.length && <div className="checkpoint-timeline">{checkpoints.map(checkpoint => {
        const sample = sampleRows.find(item => str(item.checkpoint_id) === idOf(checkpoint)) ?? sampleRows.find(item => str(item.training_step) === str(checkpoint.step));
        const readiness = checkpoint.readiness_summary && typeof checkpoint.readiness_summary === "object" ? checkpoint.readiness_summary as Row : {};
        const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
        return <button className={selectedCheckpointId === idOf(checkpoint) ? "active" : ""} aria-pressed={selectedCheckpointId === idOf(checkpoint)} key={idOf(checkpoint)} onClick={() => setSelectedCheckpointId(idOf(checkpoint))}>{sample && <AssetImage assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id)} alt="" loading="lazy" maxPixels={256} />}<strong>Step {str(checkpoint.step)}</strong><Status value={local.status ?? checkpoint.state} /><span>{str(checkpoint.filename ?? checkpoint.name)}</span></button>;
      })}</div>}
      {selectedCheckpoint && (() => {
        const readiness = selectedCheckpoint.readiness_summary && typeof selectedCheckpoint.readiness_summary === "object" ? selectedCheckpoint.readiness_summary as Row : {};
        const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
        const registered = selectedCheckpoint.registered_version && typeof selectedCheckpoint.registered_version === "object" ? selectedCheckpoint.registered_version as Row : null;
        return <div className="checkpoint-detail"><dl className="details"><dt>Artifact file</dt><dd>{str(selectedCheckpoint.filename ?? selectedCheckpoint.name)}</dd><dt>Size</dt><dd>{formatBytes(selectedCheckpoint.size)}</dd><dt>Local readiness</dt><dd><Status value={local.status ?? selectedCheckpoint.state} /> {str(local.reason)}</dd><dt>Registered version</dt><dd>{registered ? <a href={`#/model-version/${idOf(registered)}${routeQuery({ project: runView.project_id })}`}>{str(registered.name)}</a> : "Not registered"}</dd></dl><div className="actions"><a className="button vela-button" href={`#/checkpoint/${idOf(selectedCheckpoint)}${routeQuery({ project: runView.project_id })}`}><ExternalLink size={14} />Open checkpoint artifact</a>{registered && <a className="button vela-button" href={`#/model-version/${idOf(registered)}${routeQuery({ project: runView.project_id })}`}>Open registered version</a>}</div></div>;
      })()}
    </Panel>

    {!isMerge && <Panel title="Samples by checkpoint">
      <Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />
      {!samples.loading && !sampleRows.length && <div className="vela-notice" role="status"><strong>No sample images were archived</strong><span>The run can still contain valid checkpoints and metrics; its source prefix contains no sample-image artifacts to import.</span></div>}
      {!!sampleSteps.length && <div className="sample-step-strip">{sampleSteps.map(step => <a href={`#/samples/${id}${routeQuery({ step, return: `run/${id}`, project: runView.project_id })}`} key={step}><strong>Step {step}</strong><span>{sampleRows.filter(sample => str(sample.training_step, "unassigned") === step).length} images</span></a>)}</div>}
      {!!sampleRows.length && <AssetGrid assets={sampleRows} />}
    </Panel>}

    {!isMerge && <div className="dashboard-lower vela-dashboard-grid">
      <Panel title="Normalized configuration"><Notice error={config.error} loading={config.loading} />{config.data && <ConfigDetails value={(config.data.normalized ?? config.data.normalized_config ?? config.data) as Row} datasetId={datasetId} projectId={runProjectId} />}</Panel>
      <Panel title="Training config"><Notice error={config.error} loading={config.loading} />{!config.loading && <details className="config-disclosure"><summary>Show raw training configuration</summary><Json value={config.data?.raw ?? config.data?.raw_config ?? run.data?.raw_config ?? (config.data?.normalized as Row | undefined)?.training_config ?? { state: "Not imported" }} /></details>}</Panel>
    </div>}
    <Panel title="Typed lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} emptyText="No lineage indexed" emptyHint="No persisted or source-derived relationships were found for this run." />{!!rows(lineage.data).length && <LineageList value={lineage.data} />}</Panel>
  </Page>;
}

function ConfigDetails({ value, datasetId, projectId }: { value: Row; datasetId?: string; projectId?: string }) {
  const entries = Object.entries(value).filter(([key]) => key !== "training_config");
  return <><dl className="details">{entries.slice(0, 14).map(([key, item]) => <Fragment key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{key === "learning_rate" ? scientificNotation(item) : key === "dataset_sources" && datasetId ? <a href={`#/dataset/${datasetId}${routeQuery({ project: projectId })}`}>{Array.isArray(item) ? item.map(source => String(source).split("/").filter(Boolean).at(-1)).join(", ") : "Open dataset"}</a> : typeof item === "object" ? JSON.stringify(item) : str(item)}</dd></Fragment>)}</dl>{value.training_config && <details className="config-disclosure"><summary>Show complete training config</summary><Json value={value.training_config} /></details>}</>;
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
  if (!resource.data && resource.error) return <Page title="Checkpoint unavailable" subtitle={`Checkpoint ${id}`}><Notice error={resource.error} /><div className="actions"><a className="button vela-button" href={`#/runs${routeQuery({ project: checkpointProjectId })}`}>Back to runs</a><button onClick={() => void resource.reload()}>Retry</button></div></Page>;

  return <Page title={`Checkpoint step ${str(resource.data?.step, "")}`} subtitle={`${str(resource.data?.run_name, "Training run")} · Run artifact`} actions={canHydrate
    ? <button className="vela-button vela-button-primary" onClick={() => void hydrate()} disabled={hydrating || !hydrationReviewed} title={!hydrationReviewed ? "Review the transfer consequence below before hydrating" : undefined}><Download size={15} />{hydrating ? "Queuing..." : "Hydrate locally"}</button>
    : <span className="checkpoint-readiness-static" role="status"><Status value={localReadiness.status ?? resource.data?.state} />{str(localReadiness.reason)}</span>}>
    <ActionMessage message={message} error={actionError} />
    <Panel title="Run checkpoint artifact"><dl className="details"><dt>Filename</dt><dd>{str(artifact.filename, "Missing asset record")}</dd><dt>Checkpoint ID</dt><dd>{str(artifact.checkpoint_id)}</dd><dt>Asset ID</dt><dd>{str(artifact.asset_id)}</dd><dt>Revision</dt><dd>{str(artifact.checkpoint_revision_id, "No materialized revision")}</dd><dt>Training step</dt><dd>{str(artifact.step)}</dd><dt>Artifact state</dt><dd><Status value={artifact.state} /></dd><dt>Local readiness</dt><dd><Status value={localReadiness.status ?? "unknown"} /> {str(localReadiness.reason)}</dd><dt>Size</dt><dd>{formatBytes(remoteReadiness.size ?? storage[0]?.size)}</dd><dt>Storage</dt><dd>{storage.length ? storage.map(location => `${str(location.provider)} · ${str(location.hydration_state)} · ${str(location.object_key ?? location.relative_path ?? location.uri)}`).join("; ") : "No storage location recorded"}</dd><dt>SHA-256</dt><dd>{str(artifact.sha256, "Not recorded")}</dd><dt>Source run</dt><dd>{resource.data?.run_id ? <a href={`#/run/${str(resource.data.run_id)}${routeQuery({ project: checkpointProjectId })}`}>{str(resource.data.run_name, "Open run")}</a> : "Unavailable"}</dd><dt>Dataset version</dt><dd>{dataset?.dataset_id ? <a href={`#/dataset/${str(dataset.dataset_id)}${routeQuery({ project: checkpointProjectId })}`}>{str(dataset.dataset_name)} · {str(dataset.dataset_version_name)}</a> : "Not linked"}</dd></dl><details className="config-disclosure"><summary>Artifact metadata</summary><Json value={artifact.metadata ?? {}} /></details></Panel>
    {canHydrate && <section className="checkpoint-hydration-review" aria-labelledby="checkpoint-hydration-heading"><div><h2 id="checkpoint-hydration-heading">Hydration review</h2><p>Download {formatBytes(remoteReadiness.size ?? storage[0]?.size)} from {str(remoteReadiness.provider, "the recorded remote source")} into managed local storage. Existing source data is not deleted.</p></div><label><input type="checkbox" checked={hydrationReviewed} onChange={event => setHydrationReviewed(event.target.checked)} /> I reviewed the artifact size, source, and local-storage consequence.</label></section>}
    {registeredVersion ? <Panel title="Registered model version"><p>This checkpoint artifact is already registered as one intentional reusable version.</p><dl className="details"><dt>Version</dt><dd><a href={`#/model-version/${idOf(registeredVersion)}${routeQuery({ project: checkpointProjectId })}`}>{str(registeredVersion.name)}</a></dd><dt>Model</dt><dd>{registeredVersion.model_id ? <a href={`#/model/${str(registeredVersion.model_id)}${routeQuery({ project: checkpointProjectId })}`}>{str(registeredVersion.model_name, "Open model")}</a> : str(registeredVersion.model_name)}</dd><dt>Lifecycle</dt><dd><Status value={registeredVersion.lifecycle_state} /></dd></dl><a className="button vela-button" href={`#/model-version/${idOf(registeredVersion)}${routeQuery({ project: checkpointProjectId })}`}>Open registered version</a></Panel> : <Panel title="Register model version">
      <Notice error={models.error} loading={models.loading} />
      {!models.loading && !modelRows.length && <div className="vela-empty"><span>No compatible model</span><small>Create a model in this checkpoint's project before registering a version.</small><a className="button vela-button" href={`#/models${routeQuery({ project: checkpointProjectId })}`}><Plus size={14} />Create model</a></div>}
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
export function ModelsScreen({ projectId = "", params = new URLSearchParams() }: { projectId?: string; params?: URLSearchParams }) {
  const [page, setPage] = useState(() => Math.max(1, Number(params.get("page")) || 1));
  const resource = useResource<Row>(`/api/models${query({ project_id: projectId, paginated: true, limit: 50, offset: (page - 1) * 50 })}`);
  const projects = useResource<unknown>("/api/projects");
  const data = rows(resource.data);
  const total = Number(resource.data?.total ?? data.length);
  const projectRows = rows(projects.data);
  return <Page title="Models" subtitle="Registered model artifacts and their checkpoint-backed versions">
    <Panel title="Create model">
      <Notice error={projects.error} loading={projects.loading} empty={!projects.loading && !projectRows.length} />
      {!!projectRows.length && <Form submit="Create model" onSubmit={async form => {
        await api("/api/models", jsonBody({ name: form.get("name"), project_id: form.get("project_id"), description: form.get("description") || null }));
        await resource.reload();
      }}><Field label="Project"><Dropdown aria-label="Project" name="project_id" required defaultValue={projectId} options={[{ value: "", label: "Select..." }, ...projectRows.map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} /></Field><Field label="Name"><input name="name" required placeholder="Model name" /></Field><Field label="Description"><textarea name="description" placeholder="Subject, style, and intended use" /></Field></Form>}
    </Panel>
    <Panel title="Model registry"><Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data.length} />{!!data.length && <DataTable rows={data} total={total} page={page} pageSize={50} onPageChange={setPage} columns={[["name", "Name"], ["project_name", "Project"], ["base_model", "Base model"], ["latest_version", "Latest version"], ["lifecycle_state", "State"]]} onRow={row => { location.hash = `model/${idOf(row)}${routeQuery({ project: projectId })}`; }} />}</Panel>
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
  if (!resource.data && resource.error) return <Page title="Model unavailable" subtitle={`Model ${id}`}><Notice error={resource.error} /><div className="actions"><a className="button vela-button" href={`#/models${routeQuery({ project: projectId })}`}>Back to models</a><button onClick={() => void resource.reload()}>Retry</button></div></Page>;
  const readiness = latestVersion.readiness_summary && typeof latestVersion.readiness_summary === "object" ? latestVersion.readiness_summary as Row : {};
  const falReadiness = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
  const readyForGeneration = Boolean(falReadiness.available);
  return <Page title={str(resource.data?.name, "Model")} subtitle={`${str(latestVersion.base_model, "Base model unknown")} · ${str(project.data?.title ?? project.data?.name, "Project")}`} actions={<>
    {projectId && readyForGeneration ? <><a className="button vela-button vela-button-primary" href={generatorHref("image", { kind: "model", projectId, modelId: id, modelVersionId: idOf(latestVersion) })}><ImagePlus size={15} /> Generate</a><a className="button vela-button" href={`#/grids${routeQuery({ project: projectId, model: id, model_version: idOf(latestVersion) })}`}><Grid3X3 size={15} /> Grid</a></> : <button disabled title={projectId ? "Register this version with a compatible FAL endpoint before generation" : "The model is not attached to a project"}>Generation unavailable</button>}
    {idOf(latestVersion) ? <a className="button vela-button" href={`#/transfers${routeQuery({ mode: "export", model_version: idOf(latestVersion), project: projectId })}`}><Download size={15} />Export version</a> : <button disabled title="Register a model version before exporting"><Download size={15} />Export unavailable</button>}
  </>}>
    <Notice error={resource.error} loading={resource.loading} />
    <ObjectImageStrip assets={rows(preview.data)} label="Latest model outputs" />
    {resource.data && <Panel title="Artifact contract"><dl className="details"><dt>Type</dt><dd>{str(latestVersion.artifact_type, "lora").replaceAll("_", " ")}</dd><dt>Format</dt><dd>{str(latestVersion.artifact_format, "Not recorded")}</dd><dt>Method</dt><dd>{str(latestVersion.method, "Not recorded")}</dd></dl></Panel>}
    {resource.data && <Panel title="Model record"><Notice error={project.error} loading={project.loading} /><dl className="details"><dt>Description</dt><dd>{str(resource.data.description, "No description")}</dd><dt>Latest trigger words</dt><dd>{Array.isArray(latestVersion.trigger_words) && latestVersion.trigger_words.length ? latestVersion.trigger_words.join(", ") : "None registered"}</dd><dt>Base model</dt><dd>{str(latestVersion.base_model, "Not registered")}</dd><dt>Project</dt><dd>{projectId ? <a href={`#/project/${projectId}${routeQuery({})}`}>{str(project.data?.title ?? project.data?.name, "Open project")}</a> : "Not assigned"}</dd><dt>Versions</dt><dd>{versions.length}</dd></dl></Panel>}

    <Panel title="Training run data">
      <Notice error={runs.error} loading={runs.loading} empty={!runs.loading && !relatedRuns.length} />
      {relatedRuns.map((run, index) => <ModelTrainingRun key={idOf(run)} run={run} projectId={projectId} modelId={id} track={modelTrack} initiallyExpanded={index === 0} />)}
    </Panel>

    <Panel title="Eval Runs">
      <Notice error={evals.error} loading={evals.loading} empty={!evals.loading && !rows(evals.data).length} />
      {!!rows(evals.data).length && <div className="eval-run-strip">{rows(evals.data).map(evaluation => { const evalId = idOf(evaluation); const evalHref = `#/eval/${evalId}${routeQuery({ project: projectId })}`; return <article className="eval-run-card" key={evalId}><a className="eval-run-preview" href={galleryOpenHref({ ...rows(evaluation.outputs)[0], project_id: projectId, eval_run_id: evalId }, "eval_output")} aria-label={`Open ${str(evaluation.name, "eval output")} in gallery`}><div className="eval-thumbs">{rows(evaluation.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={String(output.asset_revision_id ?? output.asset_id)} alt="Eval output" maxPixels={256} />)}</div></a><a className="eval-run-info" href={evalHref}><strong>{str(evaluation.name, "Eval run")}</strong><span>{str(evaluation.plan_digest ?? evaluation.digest, "Immutable plan")} · {str(evaluation.output_count, "0")} outputs</span><Status value={evaluation.status} /></a></article>; })}</div>}
    </Panel>

    <Panel title="Registered model versions">
      <Notice loading={resource.loading} empty={!resource.loading && !versions.length} emptyText="No registered versions" emptyHint="Open a run checkpoint artifact to intentionally register a reusable model version." />
      {!!versions.length && <ModelVersionTable versions={versions} projectId={projectId} />}
    </Panel>
  </Page>;
}

function ModelVersionTable({ versions, projectId }: { versions: Row[]; projectId: string }) {
  return <>
    <div className="model-version-table-wrap" role="region" aria-label="Registered model versions" tabIndex={0}><table className="vela-table model-version-table"><thead><tr><th>Registered version</th><th>Type</th><th>Checkpoint artifact</th><th>Step</th><th>Lifecycle</th><th>Local readiness</th><th>FAL readiness</th></tr></thead><tbody>{versions.map(version => {
      const readiness = version.readiness_summary && typeof version.readiness_summary === "object" ? version.readiness_summary as Row : {};
      const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
      const fal = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
      return <tr key={idOf(version)}>
        <td><a className="model-version-link" href={`#/model-version/${idOf(version)}${routeQuery({ project: projectId })}`}><strong>{str(version.name, "Open version")}</strong><small>{idOf(version)}</small></a></td>
        <td>{str(version.artifact_type, "lora").replaceAll("_", " ")}</td>
        <td>{version.checkpoint_id ? <a href={`#/checkpoint/${str(version.checkpoint_id)}${routeQuery({ project: projectId })}`}>{str(version.filename, "Open checkpoint")}</a> : str(version.filename, "Unavailable")}</td>
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
      return <a href={`#/model-version/${idOf(version)}${routeQuery({ project: projectId })}`} key={idOf(version)}><strong>{str(version.name, "Open version")}</strong><small>{str(version.artifact_type, "lora").replaceAll("_", " ")} · {str(version.filename, "Checkpoint unavailable")} · Step {str(version.checkpoint_step, "not recorded")}</small><span><Status value={version.lifecycle_state} /><Status value={local.status ?? "unknown"} /><Status value={fal.status ?? "not registered"} /></span></a>;
    })}</div>
  </>;
}


function MergeFlowChart({ summary }: { summary: Row }) {
  const inputs = rows(summary.inputs);
  const output = (summary.output ?? {}) as Row;
  const colors: DitherColor[] = ["blue", "cyan", "teal", "green", "mint", "grey"];
  const scopeSegments = (scope: string): DitherBarSegment[] => inputs.map((input, index) => ({
    key: idOf(input) || str(input.alias),
    label: str(input.alias, `Input ${index + 1}`),
    value: Number(((input.weights ?? {}) as Row)[scope] ?? ((input.weights ?? {}) as Row).global ?? 0),
    color: colors[index % colors.length],
  }));
  return <div className="merge-flow">
    <div className="merge-flow-lanes">
      <div>{inputs.map((input, index) => <div className="merge-flow-node" key={idOf(input) || index}>
        <strong>{str(input.alias, `Input ${index + 1}`)} · step {str(input.step, "—")}</strong>
        <small>Rank {str(input.source_rank, "—")} · {str(input.role, "input")}</small>
        <small title={str(input.source_sha256)}>{str(input.source_sha256).slice(0, 12) || "SHA unavailable"}</small>
      </div>)}</div>
      <div className="merge-flow-node operator">
        <strong>{str(summary.notation, str(summary.operator, "Checkpoint merge"))}</strong>
        <small>{str(summary.operator).replaceAll("_", " ")}</small>
        <Status value={summary.status} />
      </div>
      <div className="merge-flow-node">
        <strong>{str(output.model_version_name, "Merged output")}</strong>
        <small>Rank {str(output.rank, "—")} · {str(output.dtype, "—")}</small>
        <small title={str(output.sha256)}>{str(output.sha256).slice(0, 12) || "Pending verification"}</small>
      </div>
    </div>
    <div className="merge-scope-lane"><span>Text fusion</span><DitherStackedBar segments={scopeSegments("text_fusion")} label="Text fusion merge weights" /></div>
    <div className="merge-scope-lane"><span>Transformer</span><DitherStackedBar segments={scopeSegments("transformer")} label="Transformer merge weights" /></div>
    <table className="composition-table" aria-label="Exact checkpoint merge inputs">
      <thead><tr><th>Input</th><th>Step</th><th>Rank</th><th>Text fusion</th><th>Transformer</th><th>Revision</th></tr></thead>
      <tbody>{inputs.map((input, index) => {
        const weights = (input.weights ?? {}) as Row;
        return <tr key={idOf(input) || index}><td>{str(input.alias)}</td><td>{str(input.step, "—")}</td><td>{str(input.source_rank, "—")}</td><td>{Number(weights.text_fusion ?? weights.global ?? 0) * 100}%</td><td>{Number(weights.transformer ?? weights.global ?? 0) * 100}%</td><td><code>{str(input.checkpoint_revision_id).slice(0, 8)}</code></td></tr>;
      })}</tbody>
    </table>
  </div>;
}


function ModelTrainingRun({ run, projectId, modelId, track, initiallyExpanded }: { run: Row; projectId: string; modelId: string; track: string; initiallyExpanded: boolean }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const runId = idOf(run);
  const isMerge = str(run.run_kind) === "checkpoint_merge";
  const samples = useResource<unknown>(expanded && !isMerge ? `/api/runs/${runId}/samples` : null);
  const metrics = useResource<LossMetrics>(expanded && !isMerge ? `/api/runs/${runId}/metrics?name=${encodeURIComponent(track ? `loss.${track}` : "loss")}` : null);
  const config = useResource<Row>(expanded && !isMerge ? `/api/runs/${runId}/config` : null);
  const sampleRows = rows(samples.data).filter(sample => !track || str(sample.sample_track).toLowerCase() === track).slice(0, 9);
  const mergeSummary = (run.merge_summary ?? {}) as Row;
  return <details className="training-run model-training-run" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><strong>{str(run.name, isMerge ? "Checkpoint merge" : "Training run")}</strong><span>{isMerge ? str(mergeSummary.notation, str(mergeSummary.operator, "Merge recipe")) : str(run.dataset_version_name, "No linked dataset")}</span><Status value={run.status} /></summary>
    <div className="training-run-body">
      {isMerge ? <>
        <dl className="details"><dt>Kind</dt><dd>Checkpoint merge</dd><dt>Operator</dt><dd>{str(mergeSummary.operator).replaceAll("_", " ")}</dd><dt>Inputs</dt><dd>{str(rows(mergeSummary.inputs).length, "0")}</dd><dt>Recipe digest</dt><dd><code>{str(mergeSummary.recipe_digest).slice(0, 16)}</code></dd></dl>
        <div className="actions"><a className="button vela-button" href={`#/run/${runId}${routeQuery({ project: projectId })}`}><ExternalLink size={14} />Open merge run</a>{str((mergeSummary.output as Row | undefined)?.model_version_id) && <a className="button vela-button" href={`#/model-version/${str((mergeSummary.output as Row).model_version_id)}${routeQuery({ project: projectId })}`}><ExternalLink size={14} />Open output version</a>}</div>
        {expanded && <section className="vela-section"><div className="vela-section-heading"><h3>Merge recipe and flow</h3></div><MergeFlowChart summary={mergeSummary} /></section>}
      </> : <>
        <dl className="details"><dt>Base</dt><dd>{str(run.base_model)}</dd><dt>Checkpoints</dt><dd>{str(run.checkpoint_count, "0")}</dd><dt>Samples</dt><dd>{str(run.sample_count, "0")}</dd><dt>Dataset</dt><dd>{str(run.dataset_version_name, "Not linked")}</dd></dl>
        <div className="actions"><a className="button vela-button" href={`#/run/${runId}${routeQuery({ project: projectId })}`}><ExternalLink size={14} />Open run and loss graph</a><a className="button vela-button" href={`#/samples/${runId}${routeQuery({ return: `model/${modelId}`, project: projectId })}`}><FileImage size={14} />View all samples</a></div>
        {expanded && <>
          <div className="model-training-visuals">
            <section className="vela-section model-training-chart"><div className="vela-section-heading"><h3>{track ? `${track[0].toUpperCase()}${track.slice(1)} loss` : "Loss graph"}</h3></div><Notice error={metrics.error} loading={metrics.loading} /><LossGraph points={metrics.data?.points} /></section>
            <section className="vela-section model-training-samples"><div className="vela-section-heading"><h3>Sample images</h3></div><Notice error={samples.error} loading={samples.loading} empty={!samples.loading && !sampleRows.length} />{!!sampleRows.length && <div className="model-sample-grid">{sampleRows.map(sample => <figure key={idOf(sample)}><AssetImage assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id)} alt={str(sample.name, "Training sample")} /><figcaption>Step {str(sample.training_step, "—")}</figcaption></figure>)}</div>}</section>
          </div>
          <section className="vela-section"><div className="vela-section-heading"><h3>Training configuration</h3></div><Notice error={config.error} loading={config.loading} />{config.data && <details className="config-disclosure"><summary>Show stored training configuration</summary><ModelConfigDetails value={(config.data.normalized ?? {}) as Row} raw={(config.data.raw ?? {}) as Row} datasetName={str(run.dataset_version_name, "Not linked")} /></details>}</section>
        </>}
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
  if (defaults) {
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

export { galleryOpenHref, safeReturnTarget } from "./gallery-routing";
export function metadataWithoutImageFilenames(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(metadataWithoutImageFilenames);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Row).flatMap(([key, item]) => {
    if (typeof item === "string" && /\.(?:png|jpe?g|webp|gif|avif)(?:$|[?#])/i.test(item)) return [];
    return [[key, metadataWithoutImageFilenames(item)]];
  }));
}


export function galleryHash(filters: GalleryFilters, page: number, pageSize: number, assetId = "", returnTarget = "") {
  return `#/gallery${routeQuery({
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


export function isGalleryShortcutTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest("input, textarea, select, .vela-dropdown, [contenteditable=true]"));
}

function galleryPage(value: string | null) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? Math.min(number, 1_000_000) : 1;
}
function galleryPageSize(value: string | null) {
  const number = Number(value);
  return [50, 100, 250].includes(number) ? number : 50;
}

export function GalleryScreen({ projectId = "", params = new URLSearchParams() }: { projectId?: string; params?: URLSearchParams }) {
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [page, setPage] = useState(() => galleryPage(params.get("page")));
  const [debouncedQuery, setDebouncedQuery] = useState(() => params.get("q") ?? "");
  const [pageSize, setPageSize] = useState(() => galleryPageSize(params.get("pageSize") ?? readPreference("titles.gallery.pageSize")));
  const defaultFilters: GalleryFilters = { project_id: projectId, model_id: "", dataset_id: "", eval_run_id: "", category: "", kind: "image", decision: "", rating: "", q: "", include_dataset_assets: false, sort: "origin_time_desc" };
  const [filters, setFilters] = useState<GalleryFilters>(() => ({ ...defaultFilters, ...galleryFiltersFromParams(params, projectId, defaultFilters) }));
  const models = useResource<unknown>(`/api/models${query({ project_id: filters.project_id })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: filters.project_id })}`);
  const [size, setSize] = useState(170);
  const [actionNotice, setActionNotice] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(filters.q), 300);
    return () => window.clearTimeout(timer);
  }, [filters.q]);
  const path = `/api/gallery${query({ ...filters, q: debouncedQuery, paginated: true, limit: pageSize, offset: (page - 1) * pageSize })}`;
  const resource = useResource<Row>(path);
  const assets = rows(resource.data);
  const total = Number(resource.data?.total ?? assets.length);
  const isFiltered = Object.entries(filters).some(([key, value]) => value !== defaultFilters[key as keyof typeof defaultFilters]);
  const selectedAssetId = params.get("asset") ?? "";
  const returnTarget = safeReturnTarget(params.get("return"));
  const closeHref = returnTarget ? `#/${returnTarget}` : galleryHash(filters, page, pageSize);
  const routeSignature = params.toString();
  useEffect(() => {
    setPage(galleryPage(params.get("page")));
    setDebouncedQuery(params.get("q") ?? "");
    setPageSize(galleryPageSize(params.get("pageSize") ?? readPreference("titles.gallery.pageSize")));
    setFilters({ ...defaultFilters, ...galleryFiltersFromParams(params, projectId, defaultFilters) });
  }, [routeSignature, projectId]);
  const rememberRoute = (nextFilters: GalleryFilters, nextPage: number, nextSize: number) => {
    const href = canonicalWorkspaceHref(galleryHash(nextFilters, nextPage, nextSize, selectedAssetId, returnTarget));
    history.replaceState(history.state, "", href);
  };
  const updateFilters = (next: GalleryFilters) => { setFilters(next); setPage(1); rememberRoute(next, 1, pageSize); };
  const updatePage = (next: number) => { setPage(next); rememberRoute(filters, next, pageSize); };
  const updatePageSize = (next: number) => {
    setPageSize(next); setPage(1); writePreference("titles.gallery.pageSize", String(next)); rememberRoute(filters, 1, next);
  };
  useEffect(() => {
    if (!resource.data || resource.loading || resource.error || filters.q !== debouncedQuery) return;
    const lastPage = Math.max(1, Math.ceil(total / pageSize));
    if (page > lastPage) updatePage(lastPage);
  }, [resource.data, resource.loading, resource.error, total, page, pageSize, filters.q, debouncedQuery]);
  const extraFilterCount = [filters.model_id, filters.dataset_id, filters.eval_run_id, filters.category, filters.decision, filters.rating, filters.include_dataset_assets, filters.sort !== "origin_time_desc"].filter(Boolean).length;
  return <Page title="Gallery" subtitle="Browse and review images. Open any image for ratings, comments, and metadata.">
    <Panel title="Filters" className="gallery-filters-panel">
      <Notice error={projects.error} loading={projects.loading} />
      <div className="form-grid gallery-primary-filters">
        <Field label="Search"><input value={filters.q} onChange={event => updateFilters({ ...filters, q: event.target.value })} placeholder="Filename" /></Field>
        <Field label="Project"><Dropdown aria-label="Project" value={filters.project_id} onChange={value => updateFilters({ ...filters, project_id: value, model_id: "", dataset_id: "" })} options={[{ value: "", label: "All projects" }, ...rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} /></Field>
      </div>
      <details className="gallery-more-filters">
        <summary>More filters and display{extraFilterCount > 0 && <span className="vela-meta">{extraFilterCount} active</span>}</summary>
        <div className="form-grid">
        <Field label="Model"><Dropdown aria-label="Model" value={filters.model_id} onChange={value => updateFilters({ ...filters, model_id: value })} options={[{ value: "", label: "All models" }, ...rows(models.data).map(model => ({ value: idOf(model), label: str(model.name) }))]} /></Field>
        <Field label="Dataset"><Dropdown aria-label="Dataset" value={filters.dataset_id} onChange={value => updateFilters({ ...filters, dataset_id: value })} options={[{ value: "", label: "All datasets" }, ...rows(datasets.data).map(dataset => ({ value: idOf(dataset), label: str(dataset.name) }))]} /></Field>
        <Field label="Image set"><Dropdown aria-label="Image set" value={filters.category} onChange={value => updateFilters({ ...filters, category: value, include_dataset_assets: value === "dataset_image" ? true : filters.include_dataset_assets })} options={[{ value: "", label: "All image sets" }, { value: "eval_output", label: "Grid outputs" }, { value: "sample", label: "Training samples" }, { value: "dataset_image", label: "Dataset images" }]} /></Field>
        <Field label="Decision"><Dropdown aria-label="Decision" value={filters.decision} onChange={value => updateFilters({ ...filters, decision: value })} options={[{ value: "", label: "Any" }, { value: "candidate", label: "Candidate" }, { value: "approved", label: "Approved" }, { value: "hold", label: "Hold" }, { value: "reject", label: "Reject" }]} /></Field>
        <Field label="Rating"><Dropdown aria-label="Rating" value={filters.rating} onChange={value => updateFilters({ ...filters, rating: value })} options={[{ value: "", label: "Any" }, ...[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: `${value} star${value === 1 ? "" : "s"}` }))]} /></Field>
        <Field label="Sort"><Dropdown aria-label="Sort" value={filters.sort} onChange={value => updateFilters({ ...filters, sort: value })} options={[{ value: "origin_time_desc", label: "Newest generated or modified" }, { value: "origin_time_asc", label: "Oldest generated or modified" }]} /></Field>
        <Field label="Dataset images"><span className="gallery-checkbox"><input type="checkbox" checked={filters.include_dataset_assets} onChange={event => updateFilters({ ...filters, include_dataset_assets: event.target.checked })} /> Include dataset members</span></Field>
        <Field label={`Thumbnail size · ${size}px`}><input aria-label="Thumbnail size" type="range" min="120" max="260" value={size} onChange={event => setSize(Number(event.target.value))} /></Field>
        </div>
      </details>
      <div className="actions"><button onClick={() => updateFilters(defaultFilters)} disabled={!isFiltered} title={!isFiltered ? "No additional filters are active" : undefined}><X size={14} />Clear filters</button><span className="vela-meta" aria-live="polite">{resource.loading ? "Loading images…" : `${total} matching ${total === 1 ? "image" : "images"}`}</span></div>
    </Panel>
    <Panel title="Assets" className="gallery-assets-panel">
      <ActionMessage message={actionNotice} />
      <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !assets.length} emptyText={isFiltered ? "No images match these filters" : "No images to review yet"} emptyHint={isFiltered ? "Clear filters or try a different search." : "Use + to import images, or include dataset members in More filters."} onRetry={resource.reload} />
      {!!assets.length && <div style={{ "--asset-size": `${size}px` } as CSSProperties}><AssetGrid assets={assets} hrefForAsset={asset => galleryHash(filters, page, pageSize, str(asset.asset_revision_id ?? asset.asset_id ?? asset.id), returnTarget)} /></div>}
      {total > 0 && <Pagination page={page} pageSize={pageSize} total={total} onPage={updatePage} onPageSize={updatePageSize} />}
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
  const metadataSelection = useDragTextSelection();
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
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || isGalleryShortcutTarget(event.target)) return;
      if (event.key === "ArrowLeft" && previous) { event.preventDefault(); location.hash = hrefFor(previous).slice(1); }
      if (event.key === "ArrowRight" && next) { event.preventDefault(); location.hash = hrefFor(next).slice(1); }
      if (/^[1-5]$/.test(event.key) && !ratingBusy) { event.preventDefault(); void rate(Number(event.key)); }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [id, previous && idOf(previous), next && idOf(next), ratingBusy, closeHref]);
  const originType = str(metadata.origin_type ?? (galleryContext.data?.current ? (galleryContext.data.current as Row).origin_type : undefined), "ASSET");
  const displayOriginType = originDisplayLabel(originType, metadata, asset.data ?? undefined);
  const hasTrainingMetadata = originType.toUpperCase() !== "DATASET";
  const isMerge = str(metadata.provenance_kind, "").toLowerCase() === "merge" || Boolean(metadata.merge_operation_id);
  const prompt = str(metadata.prompt ?? metadata.caption, "");
  const promptPresentation = captionPresentation({ caption: prompt, caption_format: metadata.caption_format }, str(asset.data?.name, "Selected image"));
  const replay = generationReplayFromMetadata({ ...metadata, project_id: metadata.project_id ?? asset.data?.project_id });
  const generationParameters = normalizedParametersFromMetadata(metadata);
  const providerMetadata = metadata.provider_metadata && typeof metadata.provider_metadata === "object" ? metadata.provider_metadata as Row : {};
  const providerResponse = providerMetadata.response && typeof providerMetadata.response === "object" ? providerMetadata.response as Row : {};
  const imageSeed = generationParameters.seed ?? providerResponse.seed;
  const allImageMetadata = useMemo(() => metadataWithoutImageFilenames({ asset: asset.data, image: context.data?.image, metadata, storage: context.data?.storage }), [asset.data, context.data?.image, metadata, context.data?.storage]);
  const workflow = workflowReference(metadata);
  const providerName = str(metadata.provider, relationName("provider"));
  const providerLabel = providerName.toLowerCase() === "comfyui" ? "ComfyUI" : providerName;
  return <div ref={overlay} className={`review-overlay vela-focus-workspace gallery-review-overlay ${fullscreen ? "is-fullscreen" : ""}`} role="dialog" aria-modal="true" aria-labelledby="image-review-title"><header className="viewer-topbar"><a href={closeHref}>Gallery</a><div className="viewer-actions"><button title={fullscreen ? "Exit full screen" : "Use full screen"} aria-label={fullscreen ? "Exit Full Screen" : "Full Screen"} onClick={() => { const operation = document.fullscreenElement ? document.exitFullscreen() : overlay.current?.requestFullscreen?.(); void operation?.catch(() => setRatingError("Full screen is unavailable in this browser. You can continue reviewing here.")); }}>{fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}<span>{fullscreen ? "Exit Full Screen" : "Full Screen"}</span></button><a ref={closeButton} className="button" aria-label="Close image review" href={closeHref}><X size={16} /> Close</a></div></header>
    <main className="image-review-stage"><div className="image-review-main"><nav className="image-nav" aria-label="Image navigation"><a aria-disabled={!previous} href={previous ? hrefFor(previous) : undefined}><ChevronLeft size={16} /> Previous</a><a aria-disabled={!next} href={next ? hrefFor(next) : undefined}>Next <ChevronRight size={16} /></a></nav><div className="image-review-visual"><div className="image-review-media"><AssetImage className="fit" assetRevisionId={id} variant="content" alt={promptPresentation.alt} /></div>{prompt && (promptPresentation.format === "json" ? <pre className="image-review-prompt caption-json">{promptPresentation.rendered}</pre> : <p className="image-review-prompt">{prompt}</p>)}<div className="filmstrip">{navigationItems.slice(Math.max(0, index - 4), index + 5).map(item => <a className={idOf(item) === id ? "active" : ""} href={hrefFor(item)} key={idOf(item)}><AssetImage assetRevisionId={idOf(item)} alt={str(item.name)} /></a>)}</div></div></div>
      <aside {...metadataSelection} id="storage_details" className="image-review-inspector vela-copyable-metadata">
        <header className="image-review-inspector-header">
          <h2 id="image-review-title">Image review</h2>
          <div className="image-review-inspector-actions"><span className={`asset-origin-badge origin-${originType.toLowerCase()}`}>{displayOriginType}</span><CopyMetadataButton value={allImageMetadata} /></div>
        </header>
        <Notice error={asset.error || context.error || galleryContext.error} loading={asset.loading || context.loading || galleryContext.loading} />
        <section className="image-review-meta-block" aria-labelledby="image-primary-metadata-heading">
          <div className="image-review-section-heading"><h3 id="image-primary-metadata-heading">Metadata</h3><span>{hasTrainingMetadata ? "Generation" : "Source"}</span></div>
          <dl className="details image-primary-metadata">{hasTrainingMetadata ? <><dt>Model</dt><dd>{relationName("model")}</dd><dt>Model version</dt><dd>{relationName("model_version", str(metadata.model_version_name, "Unknown"))}</dd><dt>Checkpoint</dt><dd>{relationName("checkpoint")}</dd><dt>Step</dt><dd>{str(metadata.checkpoint_step, "Not recorded")}</dd><dt>Base model</dt><dd>{relationName("base_model")}</dd><dt>Training run</dt><dd>{relationName("training_run")}</dd><dt>LoRA scale</dt><dd>{loraScaleFromMetadata(metadata) ?? "Not recorded"}</dd><dt>Seed</dt><dd>{str(imageSeed, "Not recorded")}</dd><dt>Provider</dt><dd>{providerLabel}</dd><dt>Workflow</dt><dd>{workflow?.href ? <a href={workflow.href} target="_blank" rel="noreferrer">{workflow.name}</a> : workflow?.name ?? "Not recorded"}</dd></> : <><dt>Dimensions</dt><dd>{str(metadata.width ?? (context.data?.image as Row | undefined)?.width)} × {str(metadata.height ?? (context.data?.image as Row | undefined)?.height)}</dd><dt>Dataset</dt><dd>{relationName("dataset")}</dd><dt>Provider</dt><dd>{providerLabel}</dd></>}</dl>
        </section>
        {isMerge && <section className="image-review-meta-block image-review-provenance" aria-labelledby="image-provenance-heading">
          <div className="image-review-section-heading"><h3 id="image-provenance-heading">Provenance</h3><span>Merge</span></div>
          <dl className="details image-primary-metadata"><dt>Output</dt><dd>Merge output</dd><dt>Method</dt><dd>{str(metadata.merge_operator, "Not recorded")}</dd><dt>Recipe</dt><dd>{str(metadata.merge_notation, "Not recorded")}</dd></dl>
        </section>}
        <details className="image-all-metadata"><summary>All image metadata</summary><Json value={allImageMetadata} /></details>
        <div className={`image-review-action-row ${replay ? "has-replay" : "delete-only"}`} aria-label="Image actions">
          {replay && <a className="button vela-button image-review-generate" aria-label="Generate" href={generatorHref("image", replay.context, id)}><ImagePlus size={15} /> <span>Generate</span></a>}
          <div className="image-delete-control"><button className="vela-button vela-button-danger" disabled={deleteBusy} onClick={async () => { if (!confirm(`Delete ${str(asset.data?.name, "this image")}? This removes DAM metadata and supported local content.`)) return; setDeleteBusy(true); setDeleteError(""); try { const result = await api<Row>(`/api/assets/${id}?report=true&delete_content=true`, { method: "DELETE" }); const retained = rows(result.storage).filter(item => item.status !== "deleted"); onDeleteNotice(retained.length ? `${retained.length} storage object(s) retained; DAM metadata deleted.` : "Image and local content deleted."); const destination = next ? hrefFor(next) : previous ? hrefFor(previous) : closeHref; await onDeleted(); location.hash = destination.slice(1); } catch (reason) { setDeleteError(errorText(reason)); } finally { setDeleteBusy(false); } }}><Trash2 size={15} />{deleteBusy ? "Deleting…" : "Delete image"}</button>{deleteError && <div className="vela-notice vela-notice-error" role="alert">{deleteError}</div>}</div>
        </div>
        <section className="image-review-rating-block" aria-labelledby="image-rating-heading">
          <div className="rating-control"><span id="image-rating-heading">Rating</span>{[1,2,3,4,5].map(value => <button className="vela-rating-button" aria-label={`${value} stars`} aria-pressed={currentRating === value} disabled={ratingBusy} key={value} onClick={() => void rate(value)}><Star size={16} fill={currentRating >= value ? "currentColor" : "none"} /></button>)}</div>
          {ratingError && <div className="vela-notice vela-notice-error" role="alert">{ratingError}</div>}
        </section>
        <section className="image-review-comments" aria-labelledby="image-comments-heading">
          <div className="image-review-section-heading"><h3 id="image-comments-heading">Comments</h3><span>{rows(comments.data).length} previous</span></div>
          <div className="comments">{rows(comments.data).map(comment => <article key={idOf(comment)}><strong>{str(comment.profile_name, "Operator")}</strong><p>{str(comment.body)}</p></article>)}</div>
          <Form key={`${id}/${rows(comments.data).length}`} submit="Add comment" repeatable onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: "asset", subject_id: id, body: form.get("body") })); await comments.reload(); }}><Field label="Comment"><textarea name="body" required /></Field></Form>
        </section>
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
    const displayOriginType = originDisplayLabel(originType, metadata, asset);
    const baseModel = refName("base_model");
    const model = refName("model");
    const checkpoint = refName("checkpoint");
    const originLine = [baseModel, displayOriginType].filter(Boolean).join(" · ");
    const modelLine = [model, checkpoint].filter(Boolean).join(" · ");
    const assetId = String(asset.asset_revision_id ?? asset.asset_id ?? asset.id);
    const name = str(asset.filename ?? asset.name, "Generated image");
    const loraScale = loraScaleFromMetadata(metadata);
    return <article className="asset-card" key={idOf(asset) || index}>
      <a className="asset-card-preview" href={hrefForAsset ? hrefForAsset(asset) : galleryOpenHref(asset)}><AssetImage assetRevisionId={assetId} alt={name} loading="lazy" maxPixels={256} /><span className="asset-card-name">{name}</span></a>
      <small className="asset-card-footer"><span>{originLine || "Unknown"}</span>{modelLine && <span>{modelLine}</span>}{loraScale !== null && loraScale !== undefined && <span>LoRA scale · {loraScale}</span>}</small>
    </article>;
  })}</div>;
}
