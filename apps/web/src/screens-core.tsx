import { useEffect, useState } from "react";
import { Archive, FolderSearch, Import, PlugZap, RefreshCw, RotateCcw, Settings2, Square, Upload } from "lucide-react";
import { api, idOf, jsonBody, listOf, patchBody, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Field, Form, Json, Notice, Page, Panel, ProfileSelect, SelectRecords, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

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

export function ProjectScreen({ id }: { id: string }) {
  const project = useResource<Row>(`/api/projects/${id}`);
  const activity = useResource<unknown>(`/api/activity${query({ project_id: id })}`);
  const runs = useResource<unknown>(`/api/runs${query({ project_id: id })}`);
  const datasets = useResource<unknown>(`/api/datasets${query({ project_id: id })}`);
  const models = useResource<unknown>(`/api/models${query({ project_id: id })}`);
  const evals = useResource<unknown>(`/api/eval-runs${query({ project_id: id })}`);
  const trainingImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "sample", limit: 8 })}`);
  const datasetImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "dataset_image", include_dataset_assets: true, limit: 8 })}`);
  const evalImages = useResource<unknown>(`/api/gallery${query({ project_id: id, kind: "image", category: "eval_output", limit: 8 })}`);
  const [projectMessage, setProjectMessage] = useState("");
  const [projectActionError, setProjectActionError] = useState("");
  const [projectBusy, setProjectBusy] = useState(false);
  const runRows = rows(runs.data); const datasetRows = rows(datasets.data); const modelRows = rows(models.data); const evalRows = rows(evals.data);
  const archived = project.data?.state === "archived";
  return <Page title={str(project.data?.title ?? project.data?.name, "Project")} subtitle={`${Array.isArray(project.data?.trigger_words) && project.data.trigger_words.length ? project.data.trigger_words.join(", ") : "No trigger words"} · ${str(project.data?.state, "active")}`} actions={archived ? <button disabled title="Restore the project before importing"><Upload size={15} /> Import unavailable</button> : <a className="button vela-button-primary" href={`#/import?project=${id}`}><Upload size={15} aria-hidden="true" /> Import assets</a>}>
    <Notice error={project.error || projectActionError} loading={project.loading} />
    <div className="project-visual-grid">
      <ProjectImagePanel title="Training samples" assets={rows(trainingImages.data)} loading={trainingImages.loading} error={trainingImages.error} href={projectGalleryHref(id, "sample")} returnTarget={`project/${id}`} />
      <ProjectImagePanel title="Dataset images" assets={rows(datasetImages.data)} loading={datasetImages.loading} error={datasetImages.error} href={projectGalleryHref(id, "dataset_image", { include_dataset_assets: true })} returnTarget={`project/${id}`} />
      <ProjectImagePanel title="Eval outputs" assets={rows(evalImages.data)} loading={evalImages.loading} error={evalImages.error} href={projectGalleryHref(id, "eval_output")} returnTarget={`project/${id}`} />
    </div>
    <div className="project-compare-grid">
      <Panel title="Training runs"><Notice error={runs.error} loading={runs.loading} empty={!runs.loading && !runRows.length} /><div className="comparison-stack">{runRows.slice(0, 4).map(run => <a href={`#/run/${idOf(run)}`} key={idOf(run)}><strong>{str(run.name, "Run")}</strong><Status value={run.status} /><span>{str(run.checkpoint_count, "0")} checkpoints</span></a>)}</div><a className="vela-text-button" href={`#/runs?project=${id}`}>View all runs</a></Panel>
      <Panel title="Datasets"><Notice error={datasets.error} loading={datasets.loading} empty={!datasets.loading && !datasetRows.length} /><div className="comparison-stack">{datasetRows.slice(0, 4).map(dataset => <a href={`#/dataset/${idOf(dataset)}`} key={idOf(dataset)}><strong>{str(dataset.name)}</strong><span>{str(dataset.current_version, "No version")}</span><span>{str(dataset.item_count, "0")} images</span></a>)}</div><a className="vela-text-button" href={`#/datasets?project=${id}`}>View datasets</a></Panel>
      <Panel title="Model files"><Notice error={models.error} loading={models.loading} empty={!models.loading && !modelRows.length} /><div className="comparison-stack">{modelRows.slice(0, 4).map(model => <a href={`#/model/${idOf(model)}`} key={idOf(model)}><strong>{str(model.name)}</strong><Status value={model.lifecycle_state} /><span>{str(model.latest_version, "No version")}</span></a>)}</div><a className="vela-text-button" href={`#/models?project=${id}`}>View models</a></Panel>
    </div>
    <Panel title="Run / artifact comparison"><DataTable rows={runRows} columns={[["name", "Run"], ["dataset_version_name", "Dataset"], ["model_version_name", "Model (.safetensors)"], ["latest_eval_name", "Eval batch"], ["sample_count", "Output set"]]} onRow={row => { location.hash = `run/${idOf(row)}`; }} /></Panel>
    <div className="dashboard-lower"><Panel title="Eval batches"><Notice error={evals.error} loading={evals.loading} empty={!evalRows.length} /><DataTable rows={evalRows.slice(0, 6)} columns={[["name", "Eval"], ["endpoint_adapter", "Endpoint"], ["status", "Status"], ["output_count", "Outputs"]]} onRow={row => { location.hash = `eval/${idOf(row)}`; }} /></Panel><Panel title="Project activity"><Notice error={activity.error} loading={activity.loading} empty={!rows(activity.data).length} /><DataTable rows={rows(activity.data).slice(0, 8)} columns={[["action", "Event"], ["profile_name", "Profile"], ["created_at", "Time"]]} /></Panel></div>
    {project.data && <Panel title="Project settings"><Form key={str(project.data.updated_at)} submit="Save project" onSubmit={async form => { await api(`/api/projects/${id}`, patchBody({ title: form.get("title"), description: form.get("description"), trigger_words: String(form.get("trigger_words") ?? "").split(",").map(value => value.trim()).filter(Boolean) })); setProjectMessage("Project updated."); await project.reload(); }}><div className="form-grid"><Field label="Title"><input name="title" required defaultValue={str(project.data.title)} /></Field><Field label="Trigger words" hint="Comma separated"><input name="trigger_words" defaultValue={Array.isArray(project.data.trigger_words) ? project.data.trigger_words.join(", ") : ""} /></Field></div><Field label="Description"><textarea name="description" defaultValue={str(project.data.description, "")} /></Field></Form><div className="actions"><button disabled={projectBusy} onClick={async () => { const next = archived ? "active" : "archived"; if (!archived && !window.confirm("Archive this project? Existing data is preserved.")) return; setProjectBusy(true); setProjectActionError(""); try { await api(`/api/projects/${id}`, patchBody({ state: next })); setProjectMessage(next === "active" ? "Project restored." : "Project archived."); await project.reload(); } catch (reason) { setProjectActionError(reason instanceof Error ? reason.message : String(reason)); } finally { setProjectBusy(false); } }}><Archive size={15} />{projectBusy ? "Updating..." : archived ? "Restore project" : "Archive project"}</button></div>{projectMessage && <div className="vela-notice" role="status"><strong>Project</strong><span>{projectMessage}</span></div>}</Panel>}
  </Page>;
}

function projectGalleryHref(projectId: string, category: string, extra: Record<string, unknown> = {}) {
  return `#/gallery${query({ project: projectId, category, page: 1, ...extra })}`;
}

function ProjectImagePanel({ title, assets, loading, error, href, returnTarget }: { title: string; assets: Row[]; loading: boolean; error?: string; href: string; returnTarget: string }) {
  return <section className="vela-panel project-image-panel"><div className="vela-section-heading"><h2>{title}</h2><a className="vela-text-button" href={href}>View all</a></div><Notice error={error} loading={loading} empty={!loading && !assets.length} />{!!assets.length && <div className="project-image-mosaic">{assets.slice(0, 8).map(asset => <a href={`${href}&return=${encodeURIComponent(returnTarget)}&asset=${encodeURIComponent(idOf(asset))}`} key={idOf(asset)}><AssetImage assetRevisionId={idOf(asset)} alt={str(asset.name, title)} loading="lazy" /></a>)}</div>}</section>;
}

export function ImportScreen({ initialProject = "" }: { initialProject?: string }) {
  const sourcesResource = useResource<unknown>("/api/import-sources");
  const projectsResource = useResource<unknown>("/api/projects");
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

  const canBrowse = Boolean(sourceId && !busy);
  const canInspect = Boolean(sourceId && prefix && !busy);
  const importDisabledReason = !projectId ? "Select a project before importing." : !sourceId ? "Select a storage source before importing." : !prefix ? "Choose a prefix before importing." : busy ? "Wait for the current storage operation to finish." : undefined;

  return <Page title="Import from storage" subtitle="Browse an S3 bucket, inspect any prefix, then queue a dataset or training-run import." actions={<a className="button" href="#/settings"><Settings2 size={15} aria-hidden="true" /> Manage sources</a>}>
    <Notice error={error || sourcesResource.error || projectsResource.error} />
    <Panel title="Source">
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
  const endpoints = useResource<unknown>("/api/eval-endpoints");
  const profilesResource = useResource<unknown>("/api/profiles");
  const profiles = rows(profilesResource.data);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const selectedProfile = profiles.find(profile => idOf(profile) === selectedProfileId);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [message, setMessage] = useState("");
  const [testError, setTestError] = useState("");
  const [testing, setTesting] = useState("");
  const llm = (settings.data?.llm ?? {}) as Row; const fal = (settings.data?.fal ?? {}) as Row; const credentials = (settings.data?.credentials ?? {}) as Row;
  const test = async (provider: "llm" | "fal") => {
    setMessage(""); setTestError(""); setTesting(provider);
    try {
      const result = await api<Row>(`/api/operator-settings/test/${provider}`, jsonBody({}));
      setMessage(`${provider.toUpperCase()}: ${result.ok ? "configured" : "needs configuration"}. This checks local configuration only; no provider request was sent.`);
    } catch (reason) { setTestError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setTesting(""); }
  };
  return <Page title="Settings" subtitle="Local operator identity, provider defaults, and storage connections." actions={<Status value={health.data?.ok ? "healthy" : "attention"} />}>
    <Notice error={testError || settings.error || health.error} loading={settings.loading || health.loading} />
    <Panel title="Account"><div className="form-grid"><Field label="Active comment profile"><ProfileSelect /></Field><Field label="Workspace" hint="Single local workspace in this release."><input readOnly value="Titles XYZ" /></Field></div></Panel>
    {settings.data && <Form submit="Save provider settings" onSubmit={async form => { setMessage(""); await api("/api/operator-settings", patchBody({ llm: { provider: form.get("llm_provider"), model: form.get("llm_model"), base_url: String(form.get("llm_base_url") ?? "").trim() || null }, fal: { default_endpoint: form.get("fal_endpoint"), max_parallel_jobs: Number(fal.max_parallel_jobs ?? 4) } })); setMessage("Provider settings saved."); await settings.reload(); }}>
      <Panel title="LLM"><div className="integration-heading"><Status value={credentials.llm_configured ? "key present" : "key missing"} /><button type="button" onClick={() => void test("llm")} disabled={Boolean(testing)} title={testing ? "Wait for the current configuration check to finish." : "Check model settings and environment credentials without contacting the provider."}><PlugZap size={15} aria-hidden="true" />{testing === "llm" ? "Checking..." : "Check LLM"}</button></div><div className="form-grid"><Field label="Provider"><select name="llm_provider" defaultValue={str(llm.provider, "openai")}><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="gemini">Gemini</option><option value="openai-compatible">OpenAI-compatible</option></select></Field><Field label="Model"><input name="llm_model" defaultValue={str(llm.model, "")} placeholder="gpt-5-mini" /></Field><Field label="OpenAI-compatible base URL" hint="Only required for compatible providers."><input name="llm_base_url" type="url" defaultValue={str(llm.base_url, "")} placeholder="https://api.example.com/v1" /></Field></div><p className="muted">API keys remain in environment variables and are never returned to the browser.</p></Panel>
      <Panel title="FAL"><div className="integration-heading"><Status value={credentials.fal_configured ? "key present" : "key missing"} /><button type="button" onClick={() => void test("fal")} disabled={Boolean(testing)} title={testing ? "Wait for the current configuration check to finish." : "Validate the selected adapter and environment credential without submitting a generation."}><PlugZap size={15} aria-hidden="true" />{testing === "fal" ? "Checking..." : "Check FAL"}</button></div><Notice error={endpoints.error} loading={endpoints.loading} /><div className="form-grid"><Field label="Default endpoint"><select key={`${str(fal.default_endpoint, "ideogram/v4/lora")}:${rows(endpoints.data).length}`} name="fal_endpoint" defaultValue={str(fal.default_endpoint, "ideogram/v4/lora")} disabled={endpoints.loading}>{rows(endpoints.data).map(endpoint => { const endpointId = str(endpoint.endpoint_id ?? endpoint.id); return <option value={endpointId} key={endpointId}>{str(endpoint.name ?? endpoint.endpoint_id)}</option>; })}</select></Field></div></Panel>
    </Form>}
    {message && <div className="vela-notice" role="status"><strong>Settings</strong><span>{message}</span></div>}
    <S3SourcesPanel />
    <Panel title="Local comment profiles"><Notice error={profileError || profilesResource.error} loading={profilesResource.loading} empty={!profilesResource.loading && !profiles.length} />
      <DataTable rows={profiles} columns={[["display_name", "Name"], ["email", "Email"], ["is_active", "Active"]]} onRow={profile => setSelectedProfileId(idOf(profile))} />
      {selectedProfile && <><Form key={selectedProfileId} submit="Update profile" onSubmit={async form => { await api(`/api/profiles/${selectedProfileId}`, patchBody({ display_name: form.get("display_name"), email: String(form.get("email") ?? "").trim() || null })); setMessage("Profile updated."); await profilesResource.reload(); }}><div className="form-grid"><Field label="Display name"><input name="display_name" defaultValue={str(selectedProfile.display_name)} required /></Field><Field label="Email"><input name="email" type="email" defaultValue={str(selectedProfile.email, "")} /></Field></div></Form><button type="button" disabled={profiles.length <= 1 || profileBusy} title={profiles.length <= 1 ? "At least one active comment profile is required." : "Archive this local comment profile."} onClick={async () => { if (!window.confirm(`Archive ${str(selectedProfile.display_name)}?`)) return; setProfileBusy(true); setProfileError(""); try { await api(`/api/profiles/${selectedProfileId}/archive`, jsonBody({})); setSelectedProfileId(""); setMessage("Profile archived."); await profilesResource.reload(); } catch (reason) { setProfileError(reason instanceof Error ? reason.message : String(reason)); } finally { setProfileBusy(false); } }}><Archive size={15} />{profileBusy ? "Archiving..." : "Archive profile"}</button></>}
      <Form submit="Create profile" onSubmit={async form => {
        await api("/api/profiles", jsonBody({ display_name: form.get("display_name"), email: form.get("email") || null })); await profilesResource.reload();
      }}><div className="form-grid"><Field label="Display name"><input name="display_name" required /></Field><Field label="Email"><input name="email" type="email" /></Field></div></Form>
    </Panel>
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
        <Field label="Addressing style"><select name="addressing_style" defaultValue={str(editing?.addressing_style, "auto")}><option value="auto">Auto</option><option value="path">Path</option><option value="virtual">Virtual-hosted</option></select></Field>
        <Field label="Credential environment prefix" hint="Reads PREFIX_ACCESS_KEY and PREFIX_SECRET_KEY at runtime."><input name="credential_env_prefix" defaultValue={str(editing?.credential_env_prefix, "S3")} pattern="[A-Za-z][A-Za-z0-9_]{0,39}" required /></Field>
        {editing && <Field label="Source state"><select name="is_active" defaultValue={editing.is_active === false ? "false" : "true"}><option value="true">Active</option><option value="false">Inactive</option></select></Field>}
      </div>
      <Field label="Browse starting prefixes" hint="Optional. One convenience root per line; imports can still target any key in the bucket."><textarea name="allowed_prefixes" defaultValue={Array.isArray(editing?.allowed_prefixes) ? editing.allowed_prefixes.map(String).join("\n") : ""} /></Field>
    </Form>
    <div className="actions source-actions">{editing && <><button onClick={async () => {
      setMessage(""); setTesting(true);
      try { const result = await api<Row>(`/api/import-sources/${editingId}/test`, jsonBody({})); setMessage(result.ok ? "Connection succeeded." : "Connection test did not succeed."); }
      catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
      finally { setTesting(false); }
    }} disabled={testing} title={testing ? "Connection test is running." : "List one object from the first browse hint, or from the bucket root."}><PlugZap size={15} aria-hidden="true" />{testing ? "Testing..." : "Test connection"}</button><button onClick={() => { setEditingId(""); setMessage(""); }} disabled={testing} title={testing ? "Wait for the connection test to finish." : "Return to the new-source form."}>Cancel edit</button></>}</div>
    {message && <div className="vela-notice" role="status"><strong>Storage source</strong><span>{message}</span></div>}
  </Panel>;
}
