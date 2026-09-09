import { useEffect, useMemo, useState } from "react";
import { Check, Clipboard, Download, ExternalLink, ShieldCheck, TriangleAlert } from "lucide-react";
import { api, idOf, jsonBody, listOf, routeQuery, str } from "./api";
import { useResource } from "./hooks";
import { Dropdown, Field, Notice, Page, Panel, Status } from "./ui";
import { routeWorkspaceSlug } from "./workspace-routing";

type Row = Record<string, any>;
const PACKAGED_CLI = '"/Applications/Modelfiche.app/Contents/MacOS/mfiche"';
const BASE_MODELS = [
  { value: "black-forest-labs/FLUX.1-dev", label: "FLUX.1 Dev", description: "High-quality FLUX LoRA training" },
  { value: "black-forest-labs/FLUX.1-schnell", label: "FLUX.1 Schnell", description: "Faster FLUX experiments" },
  { value: "Qwen/Qwen-Image", label: "Qwen Image", description: "Qwen image-model training" },
];
const KREA_MODELS = [
  { value: "krea/Krea-2-Raw", label: "Krea 2 Raw", description: "KEF layerwise conditioning embedding training" },
];
const TRAINERS = [
  { value: "ai-toolkit", label: "AI Toolkit", description: "General LoRA training" },
  { value: "kef-krea2", label: "KEF Krea 2", description: "Tiny layerwise conditioning embeddings" },
];
const DEFAULT_KREA_CONFIG = {
  steps: 8000, num_tokens: 5, learning_rate: 5e-4, batch_size: 1, gradient_accumulation: 1,
  resolution: 512, max_sequence_length: 512, seed: 42, log_interval: 10,
  sample_interval: "", sample_steps: 28, sample_resolution: 512,
  dtype: "bf16", transformer_storage_dtype: "fp8", gradient_checkpointing: true,
  low_vram: false, compile_transformer: true, preload_cache: true,
  timestep_distribution: "shifted_logit_normal", timestep_mu: 0, timestep_sigma: 1,
  resolution_shift: true, base_image_seq_len: 256, max_image_seq_len: 6400,
  base_shift: 0.5, max_shift: 1.15, caption_extension: ".txt", caption_mode: "paired", rebuild_cache: false,
  model_revision: "",
};

function CheckList({ checks }: { checks: Row[] }) {
  return <ul className="training-checks" aria-label="Training readiness checks">{checks.map(check => {
    const passed = check.status === "pass";
    return <li key={str(check.code)} className={passed ? "is-pass" : "is-blocked"}>
      {passed ? <Check size={18} aria-hidden="true" /> : <TriangleAlert size={18} aria-hidden="true" />}
      <div><strong>{str(check.message)}</strong>{check.fix && <small>{str(check.fix)}</small>}</div>
      <Status value={check.status} />
    </li>;
  })}</ul>;
}

export function TrainingLaunchCreateScreen({ params }: { params: URLSearchParams }) {
  const datasetId = params.get("dataset") ?? "";
  const requestedVersion = params.get("version") ?? "";
  const setupCommand = `${PACKAGED_CLI} --json --workspace ${JSON.stringify(routeWorkspaceSlug())} training setup`;
  const dataset = useResource<Row>(datasetId ? `/api/datasets/${datasetId}` : null);
  const setup = useResource<Row>("/api/training-setup");
  const [versionId, setVersionId] = useState(requestedVersion);
  const [name, setName] = useState("");
  const [trainer, setTrainer] = useState("ai-toolkit");
  const [baseModel, setBaseModel] = useState(BASE_MODELS[0].value);
  const [sourceId, setSourceId] = useState("");
  const [checkpointEvery, setCheckpointEvery] = useState(1000);
  const [backupEvery, setBackupEvery] = useState(300);
  const [maxHours, setMaxHours] = useState(24);
  const [kreaConfig, setKreaConfig] = useState(DEFAULT_KREA_CONFIG);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [setupCopied, setSetupCopied] = useState(false);

  const versions = listOf<Row>(dataset.data?.versions);
  const credentials = listOf<Row>(setup.data?.credentials);
  const sources = listOf<Row>(setup.data?.sources).filter(source => source.available);
  useEffect(() => {
    if (!versionId && dataset.data?.current_version_id) setVersionId(str(dataset.data.current_version_id));
  }, [dataset.data?.current_version_id, versionId]);
  useEffect(() => {
    if (!sourceId && sources.length === 1) setSourceId(idOf(sources[0]));
  }, [sourceId, sources]);
  function selectTrainer(value: string) {
    setTrainer(value);
    setBaseModel(value === "kef-krea2" ? KREA_MODELS[0].value : BASE_MODELS[0].value);
  }
  function setKreaNumber(key: keyof typeof DEFAULT_KREA_CONFIG, value: string) {
    setKreaConfig(current => ({ ...current, [key]: Number(value) }));
  }
  const version = versions.find(item => idOf(item) === versionId);
  const setupReady = setup.data?.ready === true;
  const canSubmit = Boolean(datasetId && versionId && name.trim() && baseModel && sourceId && setupReady && !busy);

  async function createLaunch() {
    if (!canSubmit) return;
    setBusy(true); setError("");
    const trainingConfig = trainer === "kef-krea2" ? {
      ...kreaConfig,
      checkpoint_interval: checkpointEvery,
      sample_interval: kreaConfig.sample_interval ? Number(kreaConfig.sample_interval) : null,
      model_revision: kreaConfig.model_revision || null,
    } : null;
    try {
      const launch = await api<Row>("/api/training-launches", jsonBody({
        dataset_version_id: versionId,
        source_id: sourceId,
        client_request_id: `ui-${crypto.randomUUID()}`,
        name: name.trim(),
        trainer,
        base_model: baseModel,
        output_directory: "/workspace/output",
        training_config: trainingConfig,
        checkpoint_policy: { every_n_steps: checkpointEvery },
        backup_policy: { interval_seconds: backupEvery },
        supported_endpoint_ids: [],
        expected_duration_seconds: maxHours * 3600,
      }));
      location.hash = `training/${idOf(launch)}${routeQuery({ project: (launch as Row).project_id })}`;
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }

  return <Page title="Start training" subtitle="Create one traceable trainer run from an immutable dataset version">
    <div className="training-wizard-layout">
      <div className="training-wizard-main">
        <Panel title="1. Confirm training data">
          <Notice error={dataset.error} loading={dataset.loading} />
          {!datasetId && <div className="vela-empty"><span>Choose a published dataset first</span><small>Open a dataset and select Start training.</small><a className="button vela-button" href={`#/datasets${routeQuery({ project: params.get("project") })}`}>Browse datasets</a></div>}
          {dataset.data && <>
            <dl className="details training-summary"><dt>Dataset</dt><dd>{str(dataset.data.name)}</dd><dt>Project</dt><dd>{str(dataset.data.project_name)}</dd><dt>Version</dt><dd>{str(version?.name, "Select a version")}</dd><dt>Images</dt><dd>{str(version?.item_count ?? dataset.data.item_count, "0")}</dd><dt>State</dt><dd><Status value={version?.status ?? "unknown"} /></dd></dl>
            <Field label="Published version" hint="Training remains permanently linked to this exact immutable version."><Dropdown value={versionId} onChange={setVersionId} options={versions.map(item => ({ value: idOf(item), label: `v${str(item.version_number)} · ${str(item.name)}`, description: `${str(item.item_count, "0")} images · ${str(item.caption_format, "text")} captions`, disabled: item.status !== "published" }))} /></Field>
          </>}
        </Panel>
        <Panel title="2. Describe the run">
          <div className="training-form-grid">
            <Field label="Run name" hint="Use a memorable experiment name; ModelFiche assigns the secure run ID."><input value={name} onChange={event => setName(event.target.value)} placeholder="e.g. qhoov-style-v004" /></Field>
            <Field label="Trainer"><Dropdown value={trainer} onChange={selectTrainer} options={TRAINERS} /></Field>
            <Field label="Base model"><Dropdown value={baseModel} onChange={setBaseModel} options={trainer === "kef-krea2" ? KREA_MODELS : BASE_MODELS} /></Field>
            <Field label="Checkpoint interval" hint="Training steps between checkpoint saves."><input type="number" min={1} value={checkpointEvery} onChange={event => setCheckpointEvery(Number(event.target.value))} /></Field>
            <Field label="Backup interval" hint="Seconds between remote backup passes."><input type="number" min={30} max={3600} value={backupEvery} onChange={event => setBackupEvery(Number(event.target.value))} /></Field>
            <Field label="Maximum run window" hint="Signed access expires after this many hours."><input type="number" min={1} max={720} value={maxHours} onChange={event => setMaxHours(Number(event.target.value))} /></Field>
          </div>
        </Panel>
        {trainer === "kef-krea2" && <Panel title="3. Configure KEF Krea 2">
          <div className="training-form-grid">
            <Field label="Optimizer steps"><input type="number" min={1} value={kreaConfig.steps} onChange={event => setKreaNumber("steps", event.target.value)} /></Field>
            <Field label="Embedding tokens"><input type="number" min={1} max={128} value={kreaConfig.num_tokens} onChange={event => setKreaNumber("num_tokens", event.target.value)} /></Field>
            <Field label="Learning rate"><input type="number" min="0.000000001" step="0.0001" value={kreaConfig.learning_rate} onChange={event => setKreaNumber("learning_rate", event.target.value)} /></Field>
            <Field label="Batch size"><input type="number" min={1} value={kreaConfig.batch_size} onChange={event => setKreaNumber("batch_size", event.target.value)} /></Field>
            <Field label="Gradient accumulation" hint="Microbatches per optimizer step."><input type="number" min={1} value={kreaConfig.gradient_accumulation} onChange={event => setKreaNumber("gradient_accumulation", event.target.value)} /></Field>
            <Field label="Resolution"><input type="number" min={64} max={4096} step={16} value={kreaConfig.resolution} onChange={event => setKreaNumber("resolution", event.target.value)} /></Field>
            <Field label="Maximum sequence length"><input type="number" min={1} max={4096} value={kreaConfig.max_sequence_length} onChange={event => setKreaNumber("max_sequence_length", event.target.value)} /></Field>
            <Field label="Seed"><input type="number" min={0} value={kreaConfig.seed} onChange={event => setKreaNumber("seed", event.target.value)} /></Field>
            <Field label="Compute dtype"><Dropdown value={kreaConfig.dtype} onChange={value => setKreaConfig(current => ({ ...current, dtype: value }))} options={[{ value: "bf16", label: "BF16" }, { value: "fp16", label: "FP16" }, { value: "fp32", label: "FP32" }]} /></Field>
            <Field label="Transformer storage"><Dropdown value={kreaConfig.transformer_storage_dtype} onChange={value => setKreaConfig(current => ({ ...current, transformer_storage_dtype: value, low_vram: value === "fp8" ? current.low_vram : false }))} options={[{ value: "fp8", label: "FP8" }, { value: "native", label: "Native compute dtype" }]} /></Field>
            <Field label="Model revision" hint="Optional exact 40-character commit."><input value={kreaConfig.model_revision} onChange={event => setKreaConfig(current => ({ ...current, model_revision: event.target.value.trim() }))} placeholder="40-character commit" /></Field>
            <Field label="Log interval"><input type="number" min={1} value={kreaConfig.log_interval} onChange={event => setKreaNumber("log_interval", event.target.value)} /></Field>
            <Field label="Additional sample interval" hint="Optional. Checkpoints are always sampled."><input type="number" min={1} value={kreaConfig.sample_interval} onChange={event => setKreaConfig(current => ({ ...current, sample_interval: event.target.value }))} placeholder={`Every ${checkpointEvery} checkpoint steps`} /></Field>
            <Field label="Sample denoising steps"><input type="number" min={1} value={kreaConfig.sample_steps} onChange={event => setKreaNumber("sample_steps", event.target.value)} /></Field>
            <Field label="Sample resolution"><input type="number" min={64} max={4096} step={16} value={kreaConfig.sample_resolution} onChange={event => setKreaNumber("sample_resolution", event.target.value)} /></Field>
          </div>
          <details>
            <summary>Memory and flow-matching settings</summary>
            <div className="training-form-grid">
              <Field label="Gradient checkpointing"><><input type="checkbox" checked={kreaConfig.gradient_checkpointing} onChange={event => setKreaConfig(current => ({ ...current, gradient_checkpointing: event.target.checked }))} /> Enabled</></Field>
              <Field label="Low VRAM streaming"><><input type="checkbox" disabled={kreaConfig.transformer_storage_dtype !== "fp8"} checked={kreaConfig.low_vram} onChange={event => setKreaConfig(current => ({ ...current, low_vram: event.target.checked }))} /> Enabled</></Field>
              <Field label="Compile transformer"><><input type="checkbox" checked={kreaConfig.compile_transformer} onChange={event => setKreaConfig(current => ({ ...current, compile_transformer: event.target.checked }))} /> Enabled</></Field>
              <Field label="Preload cache"><><input type="checkbox" checked={kreaConfig.preload_cache} onChange={event => setKreaConfig(current => ({ ...current, preload_cache: event.target.checked }))} /> Enabled</></Field>
              <Field label="Timestep distribution"><Dropdown value={kreaConfig.timestep_distribution} onChange={value => setKreaConfig(current => ({ ...current, timestep_distribution: value }))} options={[{ value: "shifted_logit_normal", label: "Shifted logit-normal" }, { value: "uniform", label: "Uniform" }]} /></Field>
              <Field label="Timestep mu"><input type="number" step="0.1" value={kreaConfig.timestep_mu} onChange={event => setKreaNumber("timestep_mu", event.target.value)} /></Field>
              <Field label="Timestep sigma"><input type="number" min="0.000001" step="0.1" value={kreaConfig.timestep_sigma} onChange={event => setKreaNumber("timestep_sigma", event.target.value)} /></Field>
              <Field label="Resolution shift"><><input type="checkbox" checked={kreaConfig.resolution_shift} onChange={event => setKreaConfig(current => ({ ...current, resolution_shift: event.target.checked }))} /> Enabled</></Field>
              <Field label="Base image sequence"><input type="number" min={1} value={kreaConfig.base_image_seq_len} onChange={event => setKreaNumber("base_image_seq_len", event.target.value)} /></Field>
              <Field label="Maximum image sequence"><input type="number" min={1} value={kreaConfig.max_image_seq_len} onChange={event => setKreaNumber("max_image_seq_len", event.target.value)} /></Field>
              <Field label="Base shift"><input type="number" step="0.05" value={kreaConfig.base_shift} onChange={event => setKreaNumber("base_shift", event.target.value)} /></Field>
              <Field label="Maximum shift"><input type="number" step="0.05" value={kreaConfig.max_shift} onChange={event => setKreaNumber("max_shift", event.target.value)} /></Field>
              <Field label="Caption extension"><input value={kreaConfig.caption_extension} onChange={event => setKreaConfig(current => ({ ...current, caption_extension: event.target.value }))} /></Field>
              <Field label="Caption selection" hint="Random chooses uniformly from the distinct training captions. Empty conditioning ignores every caption."><Dropdown value={kreaConfig.caption_mode} onChange={value => setKreaConfig(current => ({ ...current, caption_mode: value }))} options={[{ value: "paired", label: "Paired with each image" }, { value: "random", label: "Random caption pool (A1111-style)" }, { value: "none", label: "Empty conditioning (ignore captions)" }]} /></Field>
              <Field label="Rebuild cache"><><input type="checkbox" checked={kreaConfig.rebuild_cache} onChange={event => setKreaConfig(current => ({ ...current, rebuild_cache: event.target.checked }))} /> Enabled</></Field>
            </div>
          </details>
        </Panel>}
        <Panel title={trainer === "kef-krea2" ? "4. Secure connections" : "3. Secure connections"}>
          <Notice error={setup.error} loading={setup.loading} />
          {setup.data && <>
            <CheckList checks={listOf<Row>(setup.data.checks)} />
            <div className="training-form-grid">
              <Field label="Signing identity" hint="One signing key authenticates W&B for every Modelfiche workspace and project."><span>{credentials[0] ? `${str(credentials[0].alias)} · ${str(credentials[0].key_id)}` : "Not authenticated"}</span></Field>
              <Field label="Training storage" hint="Checkpoints and recovery files use this active destination."><Dropdown value={sourceId} onChange={setSourceId} options={sources.map(item => ({ value: idOf(item), label: str(item.name), description: str(item.bucket) }))} /></Field>
            </div>
            {!setupReady && <div className="vela-notice vela-notice-error" role="alert"><strong>Setup needs attention</strong><span>Open Terminal and run <code>{setupCommand}</code>, then use Settings for storage and resolve the remaining checks.</span><div className="actions"><button onClick={() => void navigator.clipboard.writeText(setupCommand).then(() => setSetupCopied(true)).catch(() => setError("Clipboard access was denied. Select and copy the setup command manually."))}><Clipboard size={14} />{setupCopied ? "Copied" : "Copy setup command"}</button><a className="button vela-button" href={`#/settings${routeQuery({})}`}>Open settings</a></div></div>}
          </>}
        </Panel>
        {error && <div className="vela-notice vela-notice-error" role="alert"><strong>Launch not created</strong><span>{error}</span></div>}
        <div className="training-submit-rail"><div><ShieldCheck size={20} /><span><strong>No provider work starts yet.</strong><small>Creation prepares the export and readiness checks. An agent starts compute only after preflight passes.</small></span></div><button className="vela-button vela-button-primary" disabled={!canSubmit} onClick={() => void createLaunch()}>{busy ? "Preparing…" : "Prepare training run"}</button></div>
      </div>
      <aside className="training-wizard-aside"><strong>What happens next</strong><ol><li>ModelFiche exports this exact version.</li><li>Preflight checks signing, storage, data, and telemetry configuration.</li><li>An agent generates a self-contained trainer packet.</li><li>The trainer sends losses and samples to W&B ingress and checkpoints to S3.</li><li>ModelFiche links them to this project and run.</li></ol><p>The trainer must reach W&B ingress and S3; no inbound connection to the trainer is required. Nothing billable is queued from this screen.</p></aside>
    </div>
  </Page>;
}

export function TrainingLaunchScreen({ id }: { id: string }) {
  const launch = useResource<Row>(id ? `/api/training-launches/${id}` : null);
  const preflight = useResource<Row>(id ? `/api/training-launches/${id}/preflight` : null);
  const [copied, setCopied] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const ready = preflight.data?.ready_to_start === true;
  const workspace = str(launch.data?.workspace_id, "") || routeWorkspaceSlug();
  const command = useMemo(() => `${PACKAGED_CLI} --json --workspace ${JSON.stringify(workspace)} training run prepare ${id} --output ./modelfiche-training-${id.slice(0, 8)}`, [id, workspace]);
  useEffect(() => {
    if (ready) return;
    const timer = window.setInterval(() => void Promise.all([launch.reload(), preflight.reload()]), 3000);
    return () => window.clearInterval(timer);
  }, [launch, preflight, ready]);

  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setCopied(label); }
    catch { setActionError("Clipboard access was denied. Select and copy the command manually."); }
  }
  async function cancel() {
    if (!window.confirm("Cancel this prepared training run? No trainer will be allowed to start.")) return;
    setBusy("cancel"); setActionError("");
    try { await api(`/api/training-launches/${id}/cancel`, jsonBody({})); await Promise.all([launch.reload(), preflight.reload()]); }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }
  async function remove() {
    if (!window.confirm("Delete this unstarted launch and its unused dataset export? This cannot be undone.")) return;
    setBusy("delete"); setActionError("");
    try { await api(`/api/training-launches/${id}`, { method: "DELETE" }); location.hash = `runs${routeQuery({ project: launch.data?.project_id })}`; }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : String(reason)); setBusy(""); }
  }

  return <Page title={str(launch.data?.manifest?.run && (launch.data.manifest.run as Row).name, "Training preparation")} subtitle="One readiness view from dataset export to secure AI Toolkit handoff" actions={<>{launch.data?.run_id && <a className="button vela-button" href={`#/run/${str(launch.data.run_id)}${routeQuery({ project: launch.data?.project_id })}`}><ExternalLink size={15} />Open DAM run</a>}<button className="vela-button" disabled={!launch.data || busy === "cancel"} onClick={() => void cancel()}>Cancel</button><button className="vela-button vela-button-danger" disabled={!launch.data || busy === "delete"} onClick={() => void remove()}>Delete stub</button></>}>
    <Notice error={launch.error || preflight.error} loading={launch.loading || preflight.loading} />
    {actionError && <div className="vela-notice vela-notice-error" role="alert">{actionError}</div>}
    {preflight.data && <>
      <div className={`training-readiness-hero ${ready ? "is-ready" : "is-blocked"}`}><div>{ready ? <ShieldCheck size={28} /> : <TriangleAlert size={28} />}<span><small>Launch readiness</small><strong>{ready ? "Ready for the trainer" : str(preflight.data.phase, "Preparing")}</strong><p>{ready ? "Local preflight passed. Verify trainer connectivity and actual metrics and samples after starting." : str((preflight.data.next_action as Row | undefined)?.label, "ModelFiche is preparing this run.")}</p></span></div><Status value={ready ? "ready" : "blocked"} /></div>
      <Panel title="Preflight"><CheckList checks={listOf<Row>(preflight.data.checks)} /><button className="vela-button" onClick={() => void Promise.all([launch.reload(), preflight.reload()])}>Check again</button></Panel>
      <Panel title="Agent handoff">
        <p className="training-intro">The agent runs one command on this Mac. It verifies preflight again, signs the run-scoped token locally, and creates a checksummed packet without exposing the private key to ModelFiche.</p>
        <div className="training-command"><code>{command}</code><button aria-label="Copy prepare command" onClick={() => void copy(command, "command")}><Clipboard size={16} />{copied === "command" ? "Copied" : "Copy"}</button></div>
        <div className="actions"><a className={`button vela-button ${ready ? "" : "is-disabled"}`} aria-disabled={!ready} href={ready ? `/api/training-launches/${id}/manifest` : undefined}><Download size={15} />Download redacted manifest</a></div>
        {!ready && <small>Packet generation remains locked until every required check passes.</small>}
      </Panel>
      <Panel title="Trainer transport"><dl className="details"><dt>Transport</dt><dd>Outbound object storage</dd><dt>Live telemetry</dt><dd>{launch.data?.manifest?.telemetry?.live_enabled ? "W&B losses and samples enabled" : "Disabled: archival-only training"}</dd><dt>Storage prefix</dt><dd>{str(((launch.data?.manifest as Row | undefined)?.backup as Row | undefined)?.destination && ((((launch.data?.manifest as Row).backup as Row).destination as Row).prefix))}</dd><dt>Run ID</dt><dd>{str(launch.data?.run_id)}</dd></dl><p className="training-intro">The packet contains the dataset and sync helper. The trainer needs the named S3 credentials and, for live losses and samples, access to the configured W&B ingress.</p></Panel>
    </>}
  </Page>;
}
