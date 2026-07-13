import { useEffect, useState } from "react";
import { Archive, FolderSearch, Grid3X3, ImagePlus, Import, PlugZap, RefreshCw, RotateCcw, Settings2, Sparkles, Square, Trash2, Upload, X } from "lucide-react";
import { ApiError, api, idOf, jsonBody, listOf, patchBody, query, str } from "./api";
import { ActiveTrainingPanel } from "./active-training";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Dropdown, Field, Form, Json, Notice, Page, Panel, ProfileSelect, SelectRecords, Status } from "./ui";
import { generatorHref } from "./generation";
import { LossGraph, scientificNotation } from "./screens-assets";

type Row = Record<string, unknown>;
type LocalSelection = { file: File; path: string };
type FileSystemEntryLike = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (success: (file: File) => void, failure?: (error: DOMException) => void) => void;
  createReader?: () => { readEntries: (success: (entries: FileSystemEntryLike[]) => void, failure?: (error: DOMException) => void) => void };
};
type EntryDataTransferItem = { webkitGetAsEntry?: () => FileSystemEntryLike | null };
const rows = (value: unknown) => listOf<Row>(value);

async function readLocalEntry(entry: FileSystemEntryLike, parentPath = ""): Promise<LocalSelection[]> {
  const path = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile && entry.file) {
    const file = await new Promise<File>((resolve, reject) => entry.file?.(resolve, reject));
    return [{ file, path }];
  }
  if (!entry.isDirectory || !entry.createReader) return [];
  const reader = entry.createReader();
  const entries: FileSystemEntryLike[] = [];
  while (true) {
    const batch = await new Promise<FileSystemEntryLike[]>((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    entries.push(...batch);
  }
  const nested = await Promise.all(entries.map(child => readLocalEntry(child, path)));
  return nested.flat();
}

export async function collectDroppedLocalFiles(dataTransfer: DataTransfer): Promise<LocalSelection[]> {
  const entries = Array.from(dataTransfer.items ?? []).map(item => {
    const entryItem = item as unknown as EntryDataTransferItem;
    return entryItem.webkitGetAsEntry?.();
  }).filter((entry): entry is FileSystemEntryLike => Boolean(entry));
  if (entries.length) {
    const nested = await Promise.all(entries.map(entry => readLocalEntry(entry)));
    return nested.flat();
  }
  return Array.from(dataTransfer.files ?? []).map(file => ({ file, path: file.webkitRelativePath || file.name }));
}
export const PROJECT_IMAGE_COLLECTION_ORDER = ["Dataset images", "Training samples", "Eval outputs"] as const;

export function ProjectsScreen() {
  const resource = useResource<unknown>("/api/projects");
  const projects = rows(resource.data);
  const [message, setMessage] = useState("");
  return <Page title="Projects" subtitle="Create and open the workspaces that connect datasets, training runs, models, and evaluations.">
    <Panel title="Create project"><Form submit="Create" onSubmit={async form => {
      setMessage("");
      await api("/api/projects", jsonBody({ title: form.get("name"), description: form.get("description"), trigger_words: String(form.get("trigger_words") ?? "").split(",").map(x => x.trim()).filter(Boolean) }));
      setMessage("Project created."); await resource.reload();
    }}>
      <Field label="Name"><input name="name" required /></Field>
      <Field label="Description"><textarea name="description" /></Field>
      <Field label="Trigger words" hint="Comma separated"><input name="trigger_words" /></Field>
    </Form>{message && <div className="vela-notice" role="status"><strong>Project ready</strong><span>{message}</span></div>}</Panel>
    <Panel title="All projects"><Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !projects.length} />
      <DataTable rows={projects} columns={[["title", "Name"], ["state", "State"], ["datasets_count", "Datasets"], ["runs_count", "Runs"], ["updated_at", "Updated"]]} onRow={row => { location.hash = `project/${idOf(row)}`; }} />
    </Panel>
  </Page>;
}

export function ProjectScreen({ id, editing = false }: { id: string; editing?: boolean }) {
  const project = useResource<Row>(`/api/projects/${id}`);
  const runs = useResource<unknown>(`/api/runs${query({ project_id: id })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: id })}`);
  const models = useResource<unknown>(`/api/models${query({ project_id: id })}`);
  const evals = useResource<unknown>(`/api/eval-runs${query({ project_id: id })}`);
  const trainingImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "sample", limit: 9 })}`);
  const datasetImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "dataset_image", include_dataset_assets: true, limit: 9 })}`);
  const evalImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "eval_output", limit: 9 })}`);
  const [projectMessage, setProjectMessage] = useState("");
  const [projectActionError, setProjectActionError] = useState("");
  const [projectBusy, setProjectBusy] = useState(false);
  const runRows = rows(runs.data); const datasetRows = rows(datasets.data); const modelRows = rows(models.data); const evalRows = rows(evals.data);
  const archived = project.data?.state === "archived";
  return <Page title={str(project.data?.title ?? project.data?.name, "Project")} subtitle={`${Array.isArray(project.data?.trigger_words) && project.data.trigger_words.length ? project.data.trigger_words.join(", ") : "No trigger words"}`} actions={archived ? <button disabled title="Restore the project before generating or importing"><Upload size={15} /> Actions unavailable</button> : <><a className="button vela-button vela-button-primary" href={generatorHref("image", { kind: "project", projectId: id })}><ImagePlus size={15} /> + Image</a><a className="button vela-button" href={`#/evals?project=${id}&scope=project`}><Sparkles size={15} /> Eval</a><a className="button vela-button" href={`#/grids?project=${id}&scope=project`}><Grid3X3 size={15} /> Grid</a><a className="button vela-button" href={`#/import?project=${id}`}><Upload size={15} /> Import</a><a className="button vela-button" href={`#/project/${id}?edit=1`}>Edit project</a></>}>
    <Notice error={project.error || projectActionError} loading={project.loading} />
    {archived && <a className="button vela-button" href={`#/project/${id}?edit=1`}>Edit project</a>}
    <ActiveTrainingPanel projectId={id} />
    <div className="project-visual-grid" aria-label="Project image collections">
      <ProjectImagePanel title={PROJECT_IMAGE_COLLECTION_ORDER[0]} assets={rows(datasetImages.data)} loading={datasetImages.loading} error={datasetImages.error} href={projectGalleryHref(id, "dataset_image", { include_dataset_assets: true })} returnTarget={`project/${id}`} />
      <ProjectImagePanel title={PROJECT_IMAGE_COLLECTION_ORDER[1]} assets={rows(trainingImages.data)} loading={trainingImages.loading} error={trainingImages.error} href={projectGalleryHref(id, "sample")} returnTarget={`project/${id}`} />
      <ProjectImagePanel title={PROJECT_IMAGE_COLLECTION_ORDER[2]} assets={rows(evalImages.data)} loading={evalImages.loading} error={evalImages.error} href={projectGalleryHref(id, "eval_output")} returnTarget={`project/${id}`} />
    </div>
    <LatestTrainingRun projectId={id} runs={runRows} loading={runs.loading} error={runs.error} />
    <div className="project-resource-grid">
      <Panel title="Datasets"><Notice error={datasets.error} loading={datasets.loading} /><div className="comparison-stack">{datasetRows.slice(0, 4).map(dataset => <a href={`#/dataset/${idOf(dataset)}`} key={idOf(dataset)}><strong>{str(dataset.name)}</strong><span>{str(dataset.current_version, "No version")}</span><span>{str(dataset.item_count, "0")} images</span></a>)}</div>{!datasets.loading && !datasets.error && !datasetRows.length && <div className="vela-empty"><a className="button vela-button vela-button-primary" href={`#/transfers?mode=import&project=${id}`}>Create a dataset</a></div>}<a className="vela-text-button" href={`#/datasets?project=${id}`}>View datasets</a></Panel>
      <Panel title="Model files"><Notice error={models.error} loading={models.loading} empty={!models.loading && !modelRows.length} /><div className="comparison-stack">{modelRows.slice(0, 4).map(model => <a href={`#/model/${idOf(model)}`} key={idOf(model)}><strong>{str(model.name)}</strong><Status value={model.lifecycle_state} /><span>{str(model.latest_version, "No version")}</span></a>)}</div><a className="vela-text-button" href={`#/models?project=${id}`}>View models</a></Panel>
      <Panel title="Eval batches"><Notice error={evals.error} loading={evals.loading} empty={!evalRows.length} /><DataTable rows={evalRows.slice(0, 6)} columns={[["name", "Eval"], ["endpoint_adapter", "Endpoint"], ["output_count", "Outputs"]]} onRow={row => { location.hash = `eval/${idOf(row)}`; }} /></Panel>
    </div>
    {project.data && editing && <Panel title="Edit project"><div className="vela-section-heading"><span /><a className="vela-icon-button" href={`#/project/${id}`} aria-label="Close project editor"><X size={16} /></a></div><Form key={str(project.data.updated_at)} submit="Save project" onSubmit={async form => { await api(`/api/projects/${id}`, patchBody({ title: form.get("title"), description: form.get("description"), trigger_words: String(form.get("trigger_words") ?? "").split(",").map(value => value.trim()).filter(Boolean) })); setProjectMessage("Project updated."); await project.reload(); }}><div className="form-grid"><Field label="Title"><input name="title" required defaultValue={str(project.data.title)} /></Field><Field label="Trigger words" hint="Comma separated"><input name="trigger_words" defaultValue={Array.isArray(project.data.trigger_words) ? project.data.trigger_words.join(", ") : ""} /></Field></div><Field label="Description"><textarea name="description" defaultValue={str(project.data.description, "")} /></Field></Form><div className="actions"><button disabled={projectBusy} onClick={async () => { const next = archived ? "active" : "archived"; if (!archived && !window.confirm("Archive this project? Existing data is preserved.")) return; setProjectBusy(true); setProjectActionError(""); try { await api(`/api/projects/${id}`, patchBody({ state: next })); setProjectMessage(next === "active" ? "Project restored." : "Project archived."); await project.reload(); } catch (reason) { setProjectActionError(reason instanceof Error ? reason.message : String(reason)); } finally { setProjectBusy(false); } }}><Archive size={15} />{projectBusy ? "Updating..." : archived ? "Restore project" : "Archive project"}</button></div>{projectMessage && <div className="vela-notice" role="status"><strong>Project</strong><span>{projectMessage}</span></div>}</Panel>}
  </Page>;
}

function trainingRunTimestamp(run: Row): unknown {
  const manifest = (run.raw_manifest ?? {}) as Row;
  const state = (run.raw_state ?? {}) as Row;
  const canonical = run.completed_at ?? run.started_at ?? manifest.created_at ?? state.updated_at;
  if (canonical) return canonical;
  const sourceMatch = str(run.source_prefix, "").match(/(20\d{6})[_-](\d{6})/);
  if (sourceMatch) {
    const [, date, clock] = sourceMatch;
    return `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}T${clock.slice(0, 2)}:${clock.slice(2, 4)}:${clock.slice(4, 6)}Z`;
  }
  return run.created_at;
}

export function latestTrainingRun(runs: Row[]) {
  const trainingRuns = runs.filter(run => str(run.trainer).toLowerCase() !== "checkpoint-merge");
  return [...trainingRuns].sort((left, right) => {
    const timestamp = (run: Row) => {
      const parsed = Date.parse(str(trainingRunTimestamp(run), ""));
      return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
    };
    return timestamp(right) - timestamp(left) || idOf(right).localeCompare(idOf(left));
  })[0];
}

export function LatestTrainingRun({ projectId, runs, loading, error }: { projectId: string; runs: Row[]; loading: boolean; error?: string }) {
  const latest = latestTrainingRun(runs);
  const state = str(latest?.status, "unknown").toLowerCase();
  const failed = /fail|error|cancel/.test(state);
  const completed = /complete|completed|success|succeeded|finished/.test(state);
  return <Panel title="Latest training run" className="latest-training-run-panel">
    <Notice error={error} loading={loading} />
    {!loading && !error && !latest && <div className="vela-empty"><span>No training runs yet</span><small>Imported or newly created runs will appear here.</small></div>}
    {!loading && !error && latest && <LatestTrainingRunDetails run={latest} failed={failed} completed={completed} />}
    <div className="latest-run-footer">
      {completed && Boolean(latest?.result_model_id) && <a className="latest-run-result" href={`#/model/${str(latest?.result_model_id)}`}><span>Result model</span><strong>{str(latest?.result_model_name)}</strong></a>}
      <a className="button vela-button vela-button-primary latest-run-all" href={`#/runs?project=${projectId}`}>View all training runs <span aria-hidden="true">→</span></a>
    </div>
  </Panel>;
}

function LatestTrainingRunDetails({ run, failed, completed }: { run: Row; failed: boolean; completed: boolean }) {
  const runId = idOf(run);
  const latestLoss = run.latest_loss && typeof run.latest_loss === "object" && !Array.isArray(run.latest_loss) ? run.latest_loss as Row : null;
  const lossMetric = str(latestLoss?.name, "");
  const samples = useResource<unknown>(`/api/runs/${runId}/samples`);
  const metrics = useResource<Record<string, unknown>>(lossMetric ? `/api/runs/${runId}/metrics${query({ name: lossMetric })}` : null);
  const config = useResource<Record<string, unknown>>(`/api/runs/${runId}/config`);
  const sampleRows = rows(samples.data).sort((left, right) => {
    const stepDifference = Number(right.training_step ?? right.step ?? -1) - Number(left.training_step ?? left.step ?? -1);
    if (stepDifference) return stepDifference;
    return Date.parse(str(right.generated_at ?? right.modified_at ?? right.created_at, "")) - Date.parse(str(left.generated_at ?? left.modified_at ?? left.created_at, ""));
  }).slice(0, 9);
  const timestamp = trainingRunTimestamp(run);
  const normalized = (config.data?.normalized ?? {}) as Row;
  return <div className="latest-training-run">
    <div className="latest-run-summary"><div className="comparison-stack"><a href={`#/run/${runId}`}><strong>{str(run.name, "Training run")}</strong><Status value={failed ? "failed" : completed ? "completed" : run.status} /><span>{completed ? "Completed" : "Started"} <time dateTime={str(timestamp, "")}>{formatCanonicalTimestamp(timestamp)}</time></span><span>{str(run.dataset_version_name ?? run.dataset_name, "Dataset not linked")} · {str(run.base_model, "Base model not recorded")}</span></a></div>
    <dl className="details latest-run-details"><dt>Checkpoints</dt><dd>{str(run.checkpoint_count, "0")}</dd><dt>Samples</dt><dd>{str(run.sample_count, "0")}</dd><dt>Steps</dt><dd>{str(run.current_step ?? normalized.steps ?? normalized.max_train_steps, "Not recorded")}</dd><dt>Learning rate</dt><dd>{scientificNotation(run.learning_rate ?? normalized.learning_rate)}</dd></dl></div>
    <Notice error={samples.error || metrics.error || config.error} loading={samples.loading || metrics.loading || config.loading} />
    <div className="latest-run-visuals"><div className="latest-run-loss"><LossGraph points={metrics.data?.points} /></div>
    {!!sampleRows.length && <div className="latest-run-samples" aria-label="Latest training samples">{sampleRows.map(sample => <AssetImage key={idOf(sample)} assetRevisionId={str(sample.asset_revision_id ?? sample.asset_id ?? sample.id)} alt={str(sample.prompt, "Training sample")} />)}</div>}</div>
    {failed && <div className="vela-notice vela-notice-error" role="alert"><strong>Training run failed</strong><span>Open the run to inspect its recorded status and logs.</span></div>}
  </div>;
}

export function formatCanonicalTimestamp(value: unknown) {
  const date = new Date(str(value, ""));
  return Number.isNaN(date.valueOf()) ? "Timestamp unavailable" : date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}

function projectGalleryHref(projectId: string, category: string, extra: Record<string, unknown> = {}) {
  return `#/gallery${query({ project: projectId, category, page: 1, ...extra })}`;
}

function ProjectImagePanel({ title, assets, loading, error, href, returnTarget }: { title: string; assets: Row[]; loading: boolean; error?: string; href: string; returnTarget: string }) {
  return <section className="vela-panel project-image-panel"><div className="vela-section-heading"><h2>{title}</h2><a className="vela-text-button" href={href} aria-label={`View all ${title.toLowerCase()}`}>View all</a></div><Notice error={error} loading={loading} empty={!loading && !assets.length} />{!!assets.length && <div className="project-image-mosaic">{assets.slice(0, 9).map(asset => <a href={`${href}&return=${encodeURIComponent(returnTarget)}&asset=${encodeURIComponent(idOf(asset))}`} key={idOf(asset)}><AssetImage assetRevisionId={idOf(asset)} alt={str(asset.name, title)} loading="lazy" /></a>)}</div>}</section>;
}

type ImportMode = "s3" | "local" | "chooser";

export function ImportScreen({ initialProject = "", source = "" }: { initialProject?: string; source?: string | null }) {
  const mode: ImportMode = source === "s3" || source === "local" ? source : "chooser";
  const sourcesResource = useResource<unknown>(mode === "s3" ? "/api/import-sources" : null);
  const projectsResource = useResource<unknown>(mode === "chooser" ? null : "/api/projects");
  const [sourceId, setSourceId] = useState("");
  const [projectId, setProjectId] = useState(initialProject);
  const [prefix, setPrefix] = useState("");
  const [cursor, setCursor] = useState("");
  const [browseData, setBrowseData] = useState<unknown>(null);
  const [preview, setPreview] = useState<unknown>(null);
  const [job, setJob] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [sourceTest, setSourceTest] = useState("");
  const [busy, setBusy] = useState<"browse" | "detect" | "test" | "import" | "">("");
  const [localFiles, setLocalFiles] = useState<LocalSelection[]>([]);
  const [localName, setLocalName] = useState("");
  const [localProgress, setLocalProgress] = useState(0);
  const [localResult, setLocalResult] = useState<Row | null>(null);
  const setSelectedLocalFiles = (files: LocalSelection[]) => {
    setLocalFiles(files);
    setLocalProgress(0);
    setLocalResult(null);
    setError("");
  };
  const handleLocalDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    try {
      const files = await collectDroppedLocalFiles(event.dataTransfer);
      if (!files.length) throw new Error("The dropped folder did not contain any readable files.");
      setSelectedLocalFiles(files);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const sources = rows(sourcesResource.data);
  const selectedSource = sources.find(source => idOf(source) === sourceId);
  const browseRecord = (browseData ?? {}) as Row;
  const objects = [
    ...(Array.isArray(browseRecord.prefixes) ? browseRecord.prefixes.map(value => ({ name: String(value).replace(/\/$/, "").split("/").pop() || String(value), prefix: value, kind: "prefix" })) : []),
    ...rows(browseData),
  ];

  async function browse(nextCursor = "") {
    if (!sourceId) return;
    setError(""); setBusy("browse");
    try {
      const data = await api(`/api/import-sources/${sourceId}/browse${query({ prefix, cursor: nextCursor })}`);
      setBrowseData(data); setCursor(str((data as Row)?.next_cursor, ""));
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }
  async function detect() {
    if (!sourceId || !prefix) return;
    setError(""); setBusy("detect");
    try { setPreview(await api(`/api/import-sources/${sourceId}/detect`, jsonBody({ prefix }))); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }
  async function importPrefix() {
    if (!sourceId || !projectId || !prefix) return;
    setError(""); setBusy("import");
    const detectedKind = str((preview as Row)?.kind, "");
    try { setJob(await api("/api/import-jobs", jsonBody({ source_id: sourceId, project_id: projectId, prefix, kind: detectedKind === "dataset" || detectedKind === "training_run" ? detectedKind : null }))); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }
  async function importLocalFolder() {
    if (!projectId || !localName.trim() || !localFiles.length) return;
    setError(""); setBusy("import"); setLocalResult(null); setLocalProgress(0);
    try {
      const encoded: Row[] = [];
      for (let index = 0; index < localFiles.length; index += 1) {
        const selection = localFiles[index];
        const file = selection.file;
        const dataUrl = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(reader.error); reader.onload = () => resolve(String(reader.result)); reader.readAsDataURL(file); });
        encoded.push({ path: selection.path, content_base64: dataUrl.split(",", 2)[1], mime_type: file.type || null, last_modified: file.lastModified });
        setLocalProgress(Math.round(((index + 1) / localFiles.length) * 90));
      }
      const result = await api<Row>("/api/local-imports", jsonBody({ project_id: projectId, dataset_name: localName.trim(), files: encoded }));
      setLocalResult(result); setLocalProgress(100);
    } catch (reason) {
      if (reason instanceof ApiError && reason.details && typeof reason.details === "object" && "detail" in reason.details) {
        const detail = reason.details.detail;
        if (detail && typeof detail === "object" && "failures" in detail) {
          const failures = rows(detail.failures).map(failure => `${str(failure.path, "unknown file")}: ${str(failure.error, "failed")}`);
          const message = "message" in detail ? detail.message : reason.message;
          setError(`${str(message)}${failures.length ? ` ${failures.join("; ")}` : ""}`);
        } else {
          setError(reason.message);
        }
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
    finally { setBusy(""); }
  }

  if (mode === "chooser") {
    const projectQuery = initialProject ? `&project=${encodeURIComponent(initialProject)}` : "";
    return <Page title="Import data" subtitle="Choose where the files or records should come from.">
      <Panel title="Choose an import workflow">
        <div className="actions" aria-label="Import source choices">
          <a className="button vela-button vela-button-primary" href={`#/import?source=s3${projectQuery}`}>Import from S3 storage</a>
          <a className="button vela-button" href={`#/import?source=local${projectQuery}`}>Import from local folder</a>
        </div>
      </Panel>
    </Page>;
  }

  if (mode === "local") {
    return <Page title="Import from local folder" subtitle="Create a dataset from a folder on this device.">
      <Notice error={error || projectsResource.error} />
      <Panel title="Local folder">
        <div className="form-grid"><Field label="Project"><SelectRecords name="project" records={rows(projectsResource.data)} labelKey="title" required value={projectId} onChange={setProjectId} /></Field><Field label="Dataset name"><input value={localName} onChange={event => setLocalName(event.target.value)} /></Field><Field label="Folder"><div className="local-import-dropzone" onDragOver={event => event.preventDefault()} onDrop={event => void handleLocalDrop(event)}><input type="file" multiple accept="image/*,.txt,.json" {...({ webkitdirectory: "" } as Record<string, string>)} onChange={event => { const files = Array.from(event.target.files ?? []).map(file => ({ file, path: file.webkitRelativePath || file.name })); setSelectedLocalFiles(files); }} /><p className="muted">Choose a folder or drop it here. Nested paths are preserved.</p>{localFiles.length > 0 && <p role="status">{localFiles.length} file(s) selected.</p>}</div></Field></div>
        <div className="actions"><button className="vela-button-primary" onClick={() => void importLocalFolder()} disabled={!projectId || !localName.trim() || !localFiles.length || Boolean(busy)}><Upload size={15} />{busy === "import" ? `Importing ${localProgress}%` : `Import folder (${localFiles.length} files)`}</button></div>
        {localProgress > 0 && <progress max={100} value={localProgress} aria-label="Local folder import progress" />}
        {localResult && <div className="vela-notice" role="status"><strong>{str(localResult.state)}</strong><span>{str(localResult.imported, "0")} imported, {str(localResult.duplicates, "0")} duplicates, {rows(localResult.failures).length} failures. <a href={`#/dataset/${str(localResult.dataset_id)}`}>Open dataset</a></span></div>}
        {localResult && rows(localResult.failures).length > 0 && <Json value={localResult.failures} />}
      </Panel>
    </Page>;
  }

  const canBrowse = Boolean(sourceId && !busy);
  const canInspect = Boolean(sourceId && prefix && !busy);
  const importDisabledReason = !projectId ? "Select a project before importing." : !sourceId ? "Select a storage source before importing." : !prefix ? "Choose a prefix before importing." : busy ? "Wait for the current storage operation to finish." : undefined;
  return <Page title="Import from S3 storage" subtitle="Browse an S3-compatible source, inspect a prefix, then queue a dataset or training-run import." actions={<a className="button" href="#/settings"><Settings2 size={15} aria-hidden="true" /> Manage sources</a>}>
    <Notice error={error || sourcesResource.error || projectsResource.error} />
    <Panel title="S3 storage source">
      <div className="form-grid">
        <Field label="Project"><SelectRecords name="project" records={rows(projectsResource.data)} labelKey="title" required value={projectId} onChange={setProjectId} /></Field>
        <Field label="S3 / S3-compatible source"><SelectRecords name="source" records={sources} required value={sourceId} onChange={value => {
          setSourceId(value);
          setBrowseData(null); setPreview(null); setJob(null); setCursor(""); setSourceTest("");
          const source = sources.find(item => idOf(item) === value);
          const firstPrefix = Array.isArray(source?.allowed_prefixes) ? source.allowed_prefixes[0] : "";
          setPrefix(firstPrefix ? String(firstPrefix) : "");
        }} /></Field>
        <Field label="Prefix" hint="Leave blank to browse from the bucket root."><input value={prefix} onChange={event => { setPrefix(event.target.value); setPreview(null); setJob(null); }} placeholder="training-runs/2026-07/" /></Field>
      </div>
      {selectedSource && <dl className="details source-summary"><dt>Bucket</dt><dd>{str(selectedSource.bucket)}</dd><dt>Endpoint</dt><dd>{str(selectedSource.endpoint_url, "AWS default")}</dd><dt>Region</dt><dd>{str(selectedSource.region, "Default")}</dd><dt>Browse hints</dt><dd>{Array.isArray(selectedSource.allowed_prefixes) && selectedSource.allowed_prefixes.length ? selectedSource.allowed_prefixes.map(String).join(", ") : "Whole bucket"}</dd></dl>}
      <div className="actions">
        <button className="vela-button-primary" onClick={() => void browse()} disabled={!canBrowse} title={!sourceId ? "Select a storage source first." : busy ? "A storage operation is already running." : undefined}><FolderSearch size={15} aria-hidden="true" />{busy === "browse" ? "Browsing..." : "Browse"}</button>
        <button onClick={() => void detect()} disabled={!canInspect} title={!sourceId ? "Select a storage source first." : !prefix ? "Choose a prefix first." : busy ? "A storage operation is already running." : undefined}><Import size={15} aria-hidden="true" />{busy === "detect" ? "Detecting..." : "Detect structure"}</button>
        <button onClick={async () => {
          if (!sourceId) return; setError(""); setBusy("test"); setSourceTest("");
          try { const result = await api<Row>(`/api/import-sources/${sourceId}/test`, jsonBody({})); setSourceTest(result.ok ? "Connection succeeded." : "Connection test did not succeed."); }
          catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
          finally { setBusy(""); }
        }} disabled={!sourceId || Boolean(busy)} title={!sourceId ? "Select a storage source first." : busy ? "A storage operation is already running." : undefined}><PlugZap size={15} aria-hidden="true" />{busy === "test" ? "Testing..." : "Test connection"}</button>
      </div>
      {sourceTest && <div className="vela-notice" role="status"><strong>Connection test</strong><span>{sourceTest}</span></div>}
    </Panel>
    <Panel title="Objects"><Notice loading={busy === "browse"} empty={busy !== "browse" && !objects.length} />
      <DataTable rows={objects} columns={[["name", "Name"], ["kind", "Kind"], ["size", "Size"], ["modified_at", "Modified"], ["import_state", "Import state"]]} onRow={row => {
        const selected = str(row.prefix ?? row.key ?? row.name, ""); setPrefix(selected); setPreview(null); setJob(null);
      }} />
      {cursor && <button onClick={() => void browse(cursor)} disabled={Boolean(busy)} title={busy ? "Wait for the current storage operation to finish." : undefined}>Next page</button>}
    </Panel>
    {preview !== null && <Panel title="Detection preview"><Json value={preview} /><button className="vela-button-primary" onClick={() => void importPrefix()} disabled={Boolean(importDisabledReason)} title={importDisabledReason}><Upload size={15} aria-hidden="true" />{busy === "import" ? "Queuing..." : "Start import"}</button></Panel>}
    {job !== null && <Panel title="Import queued"><div className="vela-notice" role="status"><strong>Worker job created</strong><span>The import will continue in the background.</span></div><Json value={job} />{Boolean((job as Row)?.id) && <a className="button" href={`#/jobs/${String((job as Row).id)}`}>View import job</a>}</Panel>}
  </Page>;
}

export function JobScreen({ id, general = false }: { id: string; general?: boolean }) {
  const job = useResource<Row>(`/api/${general ? "jobs" : "import-jobs"}/${id}`);
  const logs = useResource<unknown>(general ? `/api/jobs/${id}/logs` : null);
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState<"refresh" | "cancel" | "retry" | "">("");
  const state = str(general ? job.data?.state : job.data?.state ?? (job.data?.job as Row | undefined)?.state, "unknown").toLowerCase();
  const terminal = ["succeeded", "failed", "canceled"].includes(state);
  const retryable = ["failed", "canceled"].includes(state);
  async function perform(action: "refresh" | "cancel" | "retry") {
    setActionError(""); setBusy(action);
    try {
      if (action === "cancel") await api(`/api/${general ? "jobs" : "import-jobs"}/${id}/cancel`, jsonBody({}));
      if (action === "retry") await api(`/api/import-jobs/${id}/retry`, jsonBody({}));
      await job.reload();
      if (general) await logs.reload();
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }
  return <Page title={general ? "Transfer job" : "Import job"} subtitle={`Job ${id}`} actions={<><Status value={state} /><button onClick={() => void perform("refresh")} disabled={Boolean(busy)} title={busy ? "Wait for the current job action to finish." : "Reload job state and logs."}><RefreshCw size={15} aria-hidden="true" />{busy === "refresh" ? "Refreshing..." : "Refresh"}</button><button onClick={() => void perform("cancel")} disabled={terminal || Boolean(busy) || !job.data} title={!job.data ? "Job details must load before cancellation." : terminal ? "Terminal jobs cannot be canceled." : busy ? "Wait for the current job action to finish." : "Request cooperative cancellation."}><Square size={14} aria-hidden="true" />{busy === "cancel" ? "Canceling..." : "Cancel"}</button></>}>
    <Notice error={actionError || job.error} loading={job.loading} />{job.data && <Panel title="Job details"><Json value={job.data} />
      {!general && <button onClick={() => void perform("retry")} disabled={!retryable || Boolean(busy)} title={!retryable ? "Retry becomes available after an import fails or is canceled." : busy ? "Wait for the current job action to finish." : "Queue a new worker attempt using this import payload."}><RotateCcw size={15} aria-hidden="true" />{busy === "retry" ? "Retrying..." : "Retry import"}</button>}
    </Panel>}
    {general && <Panel title="Job log"><Notice error={logs.error} loading={logs.loading} empty={!logs.loading && !rows(logs.data).length} />{rows(logs.data).length > 0 && <Json value={logs.data} />}</Panel>}
  </Page>;
}

export function SettingsScreen() {
  const health = useResource<Row>("/api/system/doctor");
  const settings = useResource<Row>("/api/operator-settings");
  const workspaces = useResource<unknown>("/api/workspaces");
  const profilesResource = useResource<unknown>("/api/profiles");
  const profiles = rows(profilesResource.data);
  const workspace = rows(workspaces.data)[0];
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const selectedProfile = profiles.find(profile => idOf(profile) === selectedProfileId);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [message, setMessage] = useState("");
  const [testError, setTestError] = useState("");
  const [testing, setTesting] = useState("");
  const [falKey, setFalKey] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const llm = (settings.data?.llm ?? {}) as Row; const fal = (settings.data?.fal ?? {}) as Row; const generation = (settings.data?.generation ?? {}) as Row; const credentials = (settings.data?.credentials ?? {}) as Row;
  const [llmProvider, setLlmProvider] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModelFilter, setLlmModelFilter] = useState("");
  const [llmValidation, setLlmValidation] = useState<Row | null>(null);
  const [llmValidationError, setLlmValidationError] = useState("");
  const [providerSaving, setProviderSaving] = useState(false);
  const activeLlmProvider = llmProvider || str(llm.provider, "openai");
  useEffect(() => {
    if (!settings.data) return;
    setLlmProvider(str(llm.provider, "openai"));
    setLlmModel(str(llm.model, ""));
    setLlmBaseUrl(str(llm.base_url, ""));
  }, [settings.data]);
  const llmCatalog = useResource<Row>(
    activeLlmProvider === "openai-compatible" && !llmBaseUrl.trim()
      ? null
      : `/api/operator-settings/llm-models${query({ provider: activeLlmProvider, base_url: llmBaseUrl.trim() || undefined })}`,
  );
  const catalogCredential = (llmCatalog.data?.credential ?? {}) as Row;
  const selectedCredentialConfigured = typeof catalogCredential.configured === "boolean"
    ? catalogCredential.configured
    : activeLlmProvider === str(llm.provider) && Boolean(credentials.llm_configured);
  const credentialSource = str(catalogCredential.source, "");
  const providerLabel = ({
    openai: "OpenAI",
    openrouter: "OpenRouter",
    anthropic: "Anthropic",
    gemini: "Gemini",
    "openai-compatible": "OpenAI-compatible",
  } as Record<string, string>)[activeLlmProvider] ?? activeLlmProvider;
  const llmSelectionDirty = activeLlmProvider !== str(llm.provider, "openai")
    || llmModel !== str(llm.model, "")
    || (llmBaseUrl.trim() || null) !== (str(llm.base_url, "") || null);
  const catalogModels = rows(llmCatalog.data);
  const modelOptions = catalogModels.map(model => ({
    value: str(model.id),
    label: str(model.label, str(model.id)),
    description: [str(model.id) !== str(model.label) ? str(model.id) : "", str(model.description)].filter(Boolean).join(" · "),
  }));
  if (llmModel && !modelOptions.some(option => option.value === llmModel)) {
    modelOptions.unshift({ value: llmModel, label: llmModel, description: llmCatalog.loading ? "Loading provider catalog…" : "Saved model (not present in the current catalog)" });
  }
  const normalizedModelFilter = llmModelFilter.trim().toLowerCase();
  const visibleModelOptions = normalizedModelFilter
    ? modelOptions.filter(option => option.value === llmModel || `${option.label} ${option.value} ${option.description}`.toLowerCase().includes(normalizedModelFilter))
    : modelOptions;
  const test = async (provider: "llm" | "fal") => {
    setMessage(""); setTesting(provider);
    if (provider === "llm") {
      setLlmValidation(null);
      setLlmValidationError("");
    } else {
      setTestError("");
    }
    try {
      const body = provider === "llm"
        ? { provider: activeLlmProvider, model: llmModel, base_url: llmBaseUrl.trim() || null }
        : {};
      const result = await api<Row>(`/api/operator-settings/test/${provider}`, jsonBody(body));
      if (provider === "llm") setLlmValidation(result);
      else setMessage("FAL: credential accepted by the provider. No generation was submitted.");
    } catch (reason) {
      const error = reason instanceof Error ? reason.message : String(reason);
      if (provider === "llm") setLlmValidationError(error);
      else setTestError(error);
    } finally { setTesting(""); }
  };
  return <Page title="Settings" subtitle="Local operator identity, provider defaults, and storage connections." actions={<Status value={health.data?.ok ? "healthy" : "attention"} />}>
    <Notice error={testError || settings.error || health.error || workspaces.error} loading={settings.loading || health.loading || workspaces.loading} />
    <div className="settings-layout">
      <div className="settings-account">
    <Panel title="Account"><div className="form-grid"><Field label="Active comment profile"><ProfileSelect workspaceName={str(workspace?.name, "Workspace")} /></Field>{workspace && <Form submit="Rename workspace" onSubmit={async form => { const updated = await api<Row>(`/api/workspaces/${idOf(workspace)}`, patchBody({ name: form.get("workspace_name") })); setMessage("Workspace renamed."); workspaces.setData([updated]); window.dispatchEvent(new CustomEvent("titles:workspace-updated", { detail: updated })); }}><Field label="Workspace" hint="Single local workspace in this release."><input name="workspace_name" defaultValue={str(workspace.name)} required /></Field></Form>}</div></Panel>
      </div>
      <div className="settings-provider">
    {settings.data && <Form key={`${str(llm.provider, "openai")}:${str(llm.model, "")}:${str(llm.base_url, "")}`} submit="Save all provider settings" onSubmit={async form => { setProviderSaving(true); setMessage(""); try { await api("/api/operator-settings", patchBody({ llm: { provider: activeLlmProvider, model: llmModel, base_url: llmBaseUrl.trim() || null }, fal: { max_parallel_jobs: Number(form.get("fal_max_parallel_jobs") ?? 4) }, generation: { prompt_prepend: String(form.get("prompt_prepend") ?? ""), prompt_append: String(form.get("prompt_append") ?? "") } })); setMessage(`${providerLabel} / ${llmModel} saved as the active LLM configuration.`); await settings.reload(); } finally { setProviderSaving(false); } }}>
      <p className="muted settings-provider-status" role="status">Saved LLM configuration: <strong>{str(llm.provider, "openai")}</strong> / <strong>{str(llm.model, "model not set")}</strong></p>
      <Panel title="LLM">
        <div className="integration-heading">
          <Status value={selectedCredentialConfigured ? `${providerLabel} credential · ${credentialSource || "configured"}` : `${providerLabel} credential missing`} />
          <div className="actions">
            <button type="button" onClick={() => void llmCatalog.reload()} disabled={llmCatalog.loading || (activeLlmProvider === "openai-compatible" && !llmBaseUrl.trim())} title="Reload the selected provider's live model catalog."><RefreshCw size={15} aria-hidden="true" />{llmCatalog.loading ? "Loading models…" : "Reload models"}</button>
            <button type="button" onClick={() => void test("llm")} disabled={Boolean(testing) || !llmModel} title="Authenticate with the selected provider and verify that the chosen model exists in its live catalog."><PlugZap size={15} aria-hidden="true" />{testing === "llm" ? "Validating…" : "Validate LLM"}</button>
          </div>
        </div>
        <div className="form-grid">
          <Field label="Provider" hint="Changing provider reloads its model catalog.">
            <Dropdown name="llm_provider" aria-label="Provider" value={activeLlmProvider} options={[{ value: "openai", label: "OpenAI" }, { value: "openrouter", label: "OpenRouter" }, { value: "anthropic", label: "Anthropic" }, { value: "gemini", label: "Gemini" }, { value: "openai-compatible", label: "OpenAI-compatible" }]} onChange={provider => { setLlmProvider(provider); setLlmModel(provider === str(llm.provider) ? str(llm.model, "") : ""); setLlmBaseUrl(provider === str(llm.provider) ? str(llm.base_url, "") : ""); setLlmModelFilter(""); setLlmValidation(null); setLlmValidationError(""); }} />
          </Field>
          <Field label="Model" hint={`Pulled from the provider API. ${catalogModels.length ? `${visibleModelOptions.length} of ${catalogModels.length} models shown.` : "Caption-capable models are marked Vision when modality metadata is available."}`}>
            <div className="model-catalog-picker">
              <input type="search" aria-label="Filter models" value={llmModelFilter} onChange={event => setLlmModelFilter(event.currentTarget.value)} placeholder={`Filter ${catalogModels.length || "provider"} models…`} />
              <Dropdown name="llm_model" aria-label="Model" value={llmModel || undefined} placeholder={llmCatalog.loading ? "Loading models…" : normalizedModelFilter && visibleModelOptions.length === 0 ? "No matching models" : "Select a model…"} options={visibleModelOptions} onChange={value => { setLlmModel(value); setLlmModelFilter(""); setLlmValidation(null); setLlmValidationError(""); }} required />
            </div>
          </Field>
          <Field label="API base URL" hint={activeLlmProvider === "openai-compatible" ? "Required. Enter the provider's OpenAI-compatible /v1 endpoint, then reload models." : "Optional override. Leave blank to use the selected provider's standard endpoint."}>
            <input name="llm_base_url" type="url" value={llmBaseUrl} onChange={event => { setLlmBaseUrl(event.currentTarget.value); setLlmValidation(null); setLlmValidationError(""); }} placeholder={activeLlmProvider === "openai-compatible" ? "https://api.example.com/v1" : "Provider default"} />
          </Field>
        </div>
        {llmSelectionDirty && <div className="vela-notice" role="status"><strong>Unsaved LLM selection</strong><span>{providerLabel} / {llmModel || "select a model"}</span></div>}
        <div className="actions llm-save-actions">
          <button type="submit" className="primary" disabled={providerSaving || !llmModel}>{providerSaving ? "Saving LLM configuration…" : "Save LLM configuration"}</button>
        </div>
        <Notice error={llmCatalog.error} loading={llmCatalog.loading} empty={!llmCatalog.loading && !llmCatalog.error && modelOptions.length === 0} emptyText={activeLlmProvider === "openai-compatible" && !llmBaseUrl.trim() ? "Enter a base URL to load models." : "The provider returned no models."} />
        {llmValidationError && <div className="vela-notice vela-notice-error" role="alert"><strong>{providerLabel} validation failed</strong><span>{llmValidationError}</span></div>}
        {llmValidation && <div className="vela-notice" role="status"><strong>{providerLabel} validated</strong><span>{str(llmValidation.model, llmModel)} was returned by {providerLabel}; credential source: {str(llmValidation.credential_source, credentialSource || "configured")}.</span></div>}
        <Field label={`${providerLabel} API key`} hint="Write-only. Save the key, reload models, then validate the selected model.">
          <input type="password" autoComplete="new-password" value={llmKey} onChange={event => setLlmKey(event.currentTarget.value)} placeholder={selectedCredentialConfigured ? `${providerLabel} credential configured — enter a replacement` : `Enter ${providerLabel} API key`} />
        </Field>
        <div className="actions">
          <button type="button" className="primary" disabled={!llmKey.trim()} onClick={async () => { setLlmValidation(null); setLlmValidationError(""); try { await api("/api/operator-settings/llm-key", { method: "PUT", body: JSON.stringify({ key: llmKey }) }); setLlmKey(""); setMessage(`${providerLabel} key saved. Provider and model selection preserved.`); await llmCatalog.reload(); } catch (reason) { setLlmValidationError(reason instanceof Error ? reason.message : String(reason)); } }}>Save key</button>
          <button type="button" disabled={!selectedCredentialConfigured} onClick={async () => { setLlmValidation(null); setLlmValidationError(""); try { await api("/api/operator-settings/llm-key", { method: "DELETE" }); setLlmKey(""); setMessage(`Saved ${providerLabel} key cleared. Environment credentials, if present, still take precedence.`); await llmCatalog.reload(); } catch (reason) { setLlmValidationError(reason instanceof Error ? reason.message : String(reason)); } }}><Trash2 size={14} />Clear saved key</button>
        </div>
        <p className="muted">{activeLlmProvider === "openrouter" ? "OpenRouter uses OPENROUTER_API_KEY (or LLM_API_KEY) when an environment credential is present." : "Environment credentials take precedence over the write-only saved key."}</p>
      </Panel>
      <Panel title="FAL worker"><Field label="Maximum parallel jobs"><input name="fal_max_parallel_jobs" type="number" min="1" max="32" defaultValue={str(fal.max_parallel_jobs, "4")} /></Field></Panel>
      <Panel title="Generation prompts"><p className="muted">Applied to every Image, Eval, and Grid prompt at compile time. Leave either field empty to disable that affix.</p><div className="form-grid"><Field label="Prompt prepend" hint="Inserted before every submitted prompt."><textarea name="prompt_prepend" defaultValue={str(generation.prompt_prepend, "")} /></Field><Field label="Prompt append" hint="Inserted after every submitted prompt."><textarea name="prompt_append" defaultValue={str(generation.prompt_append, "")} /></Field></div></Panel>
    </Form>}
      </div>
      <div className="settings-credential">
    {settings.data && <Panel title="FAL credential"><div className="integration-heading"><Status value={credentials.fal_configured ? "key present" : "key missing"} /><button type="button" onClick={() => void test("fal")} disabled={Boolean(testing) || !credentials.fal_configured} title="Authenticate against FAL without submitting a generation."><PlugZap size={15} aria-hidden="true" />{testing === "fal" ? "Validating..." : "Validate connection"}</button></div><Field label="FAL API key" hint="Write-only. Saved keys are never returned to this page."><input type="password" autoComplete="new-password" value={falKey} onChange={event => setFalKey(event.currentTarget.value)} placeholder={credentials.fal_configured ? "Configured — enter a replacement" : "Enter FAL key"} /></Field><div className="actions"><button type="button" className="primary" disabled={!falKey.trim()} onClick={async () => { setTestError(""); try { await api("/api/operator-settings/fal-key", { method: "PUT", body: JSON.stringify({ key: falKey }) }); setFalKey(""); setMessage("FAL key saved. Validate the connection to confirm provider access."); await settings.reload(); } catch (reason) { setTestError(reason instanceof Error ? reason.message : String(reason)); } }}>Save key</button><button type="button" disabled={!credentials.fal_configured} onClick={async () => { setTestError(""); try { await api("/api/operator-settings/fal-key", { method: "DELETE" }); setFalKey(""); setMessage("Saved FAL key cleared. Environment credentials, if present, still take precedence."); await settings.reload(); } catch (reason) { setTestError(reason instanceof Error ? reason.message : String(reason)); } }}><Trash2 size={14} />Clear saved key</button></div><p className="muted">Source: {str((settings.data.fal_connection as Row | undefined)?.source, "not configured")} · validation: {str(((settings.data.fal_connection as Row | undefined)?.last_validation as Row | undefined)?.state, "not run")}</p></Panel>}
      </div>
    {message && <div className="vela-notice" role="status"><strong>Settings</strong><span>{message}</span></div>}
      <div className="settings-storage">
    <S3SourcesPanel />
      </div>
      <div className="settings-profiles">
    <Panel title="Local comment profiles"><Notice error={profileError || profilesResource.error} loading={profilesResource.loading} empty={!profilesResource.loading && !profiles.length} />
      <DataTable rows={profiles} columns={[["display_name", "Name"], ["email", "Email"], ["is_active", "Active"]]} onRow={profile => setSelectedProfileId(idOf(profile))} />
      {selectedProfile && <><Form key={selectedProfileId} submit="Update profile" onSubmit={async form => { await api(`/api/profiles/${selectedProfileId}`, patchBody({ display_name: form.get("display_name"), email: String(form.get("email") ?? "").trim() || null })); setMessage("Profile updated."); await profilesResource.reload(); }}><div className="form-grid"><Field label="Display name"><input name="display_name" defaultValue={str(selectedProfile.display_name)} required /></Field><Field label="Email"><input name="email" type="email" defaultValue={str(selectedProfile.email, "")} /></Field></div></Form><button type="button" disabled={profiles.length <= 1 || profileBusy} title={profiles.length <= 1 ? "At least one active comment profile is required." : "Archive this local comment profile."} onClick={async () => { if (!window.confirm(`Archive ${str(selectedProfile.display_name)}?`)) return; setProfileBusy(true); setProfileError(""); try { await api(`/api/profiles/${selectedProfileId}/archive`, jsonBody({})); setSelectedProfileId(""); setMessage("Profile archived."); await profilesResource.reload(); } catch (reason) { setProfileError(reason instanceof Error ? reason.message : String(reason)); } finally { setProfileBusy(false); } }}><Archive size={15} />{profileBusy ? "Archiving..." : "Archive profile"}</button></>}
      <Form submit="Create profile" onSubmit={async form => {
        await api("/api/profiles", jsonBody({ display_name: form.get("display_name"), email: form.get("email") || null })); await profilesResource.reload();
      }}><div className="form-grid"><Field label="Display name"><input name="display_name" required /></Field><Field label="Email"><input name="email" type="email" /></Field></div></Form>
    </Panel>
      </div>
    </div>
  </Page>;
}

function S3SourcesPanel() {
  const resource = useResource<unknown>("/api/import-sources");
  const sources = rows(resource.data);
  const [editingId, setEditingId] = useState("");
  const [message, setMessage] = useState("");
  const [testing, setTesting] = useState(false);
  const editing = sources.find(source => idOf(source) === editingId);

  async function save(form: FormData) {
    const payload: Row = {
      name: form.get("name"),
      endpoint_url: String(form.get("endpoint_url") ?? "").trim() || null,
      bucket: form.get("bucket"),
      region: String(form.get("region") ?? "").trim() || null,
      addressing_style: form.get("addressing_style"),
      credential_env_prefix: String(form.get("credential_env_prefix") ?? "S3").trim().toUpperCase(),
      allowed_prefixes: String(form.get("allowed_prefixes") ?? "").split(/[\n,]/).map(value => value.trim()).filter(Boolean),
    };
    if (editingId) payload.is_active = form.get("is_active") === "true";
    if (editingId) await api(`/api/import-sources/${editingId}`, patchBody(payload));
    else await api("/api/import-sources", jsonBody(payload));
    setMessage(editingId ? "Source updated." : "Source created."); setEditingId(""); await resource.reload();
  }

  return <Panel title="S3 / S3-compatible sources">
    <p className="muted">Connections are read-only. Credential values stay in process environment variables and are never saved here. Select a table row to edit or test it.</p>
    <Notice error={resource.error} loading={resource.loading} empty={!sources.length} />
    <DataTable rows={sources} columns={[["name", "Name"], ["bucket", "Bucket"], ["endpoint_url", "Endpoint"], ["region", "Region"], ["addressing_style", "Addressing"], ["credential_env_prefix", "Credential prefix"], ["is_active", "Active"]]} onRow={row => { setEditingId(idOf(row)); setMessage(""); }} />
    <div className="vela-section-heading"><h2>{editing ? `Edit ${str(editing.name)}` : "Add source"}</h2></div>
    <Form key={editingId || "new"} submit={editing ? "Update source" : "Create source"} onSubmit={save}>
      <div className="form-grid">
        <Field label="Name"><input name="name" defaultValue={str(editing?.name, "")} required /></Field>
        <Field label="Bucket"><input name="bucket" defaultValue={str(editing?.bucket, "")} required /></Field>
        <Field label="Endpoint URL" hint="Leave blank for standard AWS S3."><input name="endpoint_url" type="url" defaultValue={str(editing?.endpoint_url, "")} placeholder="https://s3.example.com" /></Field>
        <Field label="Region"><input name="region" defaultValue={str(editing?.region, "")} placeholder="us-east-1" /></Field>
        <Field label="Addressing style"><Dropdown name="addressing_style" aria-label="Addressing style" defaultValue={str(editing?.addressing_style, "auto")} options={[{ value: "auto", label: "Auto" }, { value: "path", label: "Path" }, { value: "virtual", label: "Virtual-hosted" }]} /></Field>
        <Field label="Credential environment prefix" hint="Reads PREFIX_ACCESS_KEY and PREFIX_SECRET_KEY at runtime."><input name="credential_env_prefix" defaultValue={str(editing?.credential_env_prefix, "S3")} pattern="[A-Za-z][A-Za-z0-9_]{0,39}" required /></Field>
        {editing && <Field label="Source state"><Dropdown name="is_active" aria-label="Source state" defaultValue={editing.is_active === false ? "false" : "true"} options={[{ value: "true", label: "Active" }, { value: "false", label: "Inactive" }]} /></Field>}
      </div>
      <Field label="Browse starting prefixes" hint="Optional. One convenience root per line; imports can still target any key in the bucket."><textarea name="allowed_prefixes" defaultValue={Array.isArray(editing?.allowed_prefixes) ? editing.allowed_prefixes.map(String).join("\n") : ""} /></Field>
    </Form>
    <div className="actions source-actions">{editing && <><button onClick={async () => {
      setMessage(""); setTesting(true);
      try { const result = await api<Row>(`/api/import-sources/${editingId}/test`, jsonBody({})); setMessage(result.ok ? "Connection succeeded." : "Connection test did not succeed."); }
      catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
      finally { setTesting(false); }
    }} disabled={testing} title={testing ? "Connection test is running." : "List one object from the first browse hint, or from the bucket root."}><PlugZap size={15} aria-hidden="true" />{testing ? "Testing..." : "Test connection"}</button><button onClick={async () => {
      if (!window.confirm(`Disconnect source ${str(editing.name)}? This disables the connection but does not delete the bucket or its objects.`)) return;
      setMessage("");
      try { await api(`/api/import-sources/${editingId}`, { method: "DELETE" }); setEditingId(""); setMessage("Source disconnected."); await resource.reload(); }
      catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
    }} disabled={testing} title="Disconnect this source without deleting bucket data."><PlugZap size={15} />Disconnect source</button><button onClick={() => { setEditingId(""); setMessage(""); }} disabled={testing} title={testing ? "Wait for the connection test to finish." : "Return to the new-source form."}>Cancel edit</button></>}</div>
    {message && <div className="vela-notice" role="status"><strong>Storage source</strong><span>{message}</span></div>}
  </Panel>;
}
