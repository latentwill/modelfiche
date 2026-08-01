import { useEffect, useMemo, useState } from "react";
import { Check, Clipboard, Download, ExternalLink, ShieldCheck, TriangleAlert } from "lucide-react";
import { api, idOf, jsonBody, listOf, routeQuery, str } from "./api";
import { useResource } from "./hooks";
import { Dropdown, Field, Notice, Page, Panel, Status } from "./ui";

type Row = Record<string, any>;
const PACKAGED_CLI = '"/Applications/Modelfiche.app/Contents/MacOS/mfiche"';
const SETUP_COMMAND = `${PACKAGED_CLI} --json training setup`;
const BASE_MODELS = [
  { value: "black-forest-labs/FLUX.1-dev", label: "FLUX.1 Dev", description: "High-quality FLUX LoRA training" },
  { value: "black-forest-labs/FLUX.1-schnell", label: "FLUX.1 Schnell", description: "Faster FLUX experiments" },
  { value: "Qwen/Qwen-Image", label: "Qwen Image", description: "Qwen image-model training" },
];

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
  const dataset = useResource<Row>(datasetId ? `/api/datasets/${datasetId}` : null);
  const setup = useResource<Row>("/api/training-setup");
  const [versionId, setVersionId] = useState(requestedVersion);
  const [name, setName] = useState("");
  const [baseModel, setBaseModel] = useState(BASE_MODELS[0].value);
  const [credentialId, setCredentialId] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [checkpointEvery, setCheckpointEvery] = useState(1000);
  const [backupEvery, setBackupEvery] = useState(300);
  const [maxHours, setMaxHours] = useState(24);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [setupCopied, setSetupCopied] = useState(false);

  const versions = listOf<Row>(dataset.data?.versions);
  const credentials = listOf<Row>(setup.data?.credentials);
  const sources = listOf<Row>(setup.data?.sources).filter(source => source.verified);
  useEffect(() => {
    if (!versionId && dataset.data?.current_version_id) setVersionId(str(dataset.data.current_version_id));
  }, [dataset.data?.current_version_id, versionId]);
  useEffect(() => {
    if (!credentialId && credentials.length === 1) setCredentialId(idOf(credentials[0]));
    if (!sourceId && sources.length === 1) setSourceId(idOf(sources[0]));
  }, [credentialId, credentials, sourceId, sources]);
  const version = versions.find(item => idOf(item) === versionId);
  const setupReady = setup.data?.ready === true;
  const canSubmit = Boolean(datasetId && versionId && name.trim() && baseModel && credentialId && sourceId && setupReady && !busy);

  async function createLaunch() {
    if (!canSubmit) return;
    setBusy(true); setError("");
    try {
      const launch = await api<Row>("/api/training-launches", jsonBody({
        dataset_version_id: versionId,
        wandb_credential_id: credentialId,
        source_id: sourceId,
        client_request_id: `ui-${crypto.randomUUID()}`,
        name: name.trim(),
        trainer: "ai-toolkit",
        base_model: baseModel,
        output_directory: "/workspace/output",
        checkpoint_policy: { every_n_steps: checkpointEvery },
        backup_policy: { interval_seconds: backupEvery },
        supported_endpoint_ids: [],
        expected_duration_seconds: maxHours * 3600,
      }));
      location.hash = `training/${idOf(launch)}${routeQuery({ project: (launch as Row).project_id })}`;
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }

  return <Page title="Start training" subtitle="Create one traceable AI Toolkit run from an immutable dataset version">
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
            <Field label="Base model"><Dropdown value={baseModel} onChange={setBaseModel} options={BASE_MODELS} /></Field>
            <Field label="Checkpoint interval" hint="Training steps between checkpoint saves."><input type="number" min={1} value={checkpointEvery} onChange={event => setCheckpointEvery(Number(event.target.value))} /></Field>
            <Field label="Backup interval" hint="Seconds between remote backup passes."><input type="number" min={30} max={3600} value={backupEvery} onChange={event => setBackupEvery(Number(event.target.value))} /></Field>
            <Field label="Maximum run window" hint="Signed access expires after this many hours."><input type="number" min={1} max={720} value={maxHours} onChange={event => setMaxHours(Number(event.target.value))} /></Field>
          </div>
        </Panel>
        <Panel title="3. Secure connections">
          <Notice error={setup.error} loading={setup.loading} />
          {setup.data && <>
            <CheckList checks={listOf<Row>(setup.data.checks)} />
            <div className="training-form-grid">
              <Field label="Signing identity" hint="ModelFiche stores only the public key; the agent keeps the private key."><Dropdown value={credentialId} onChange={setCredentialId} options={credentials.map(item => ({ value: idOf(item), label: str(item.alias), description: str(item.key_id) }))} /></Field>
              <Field label="Training storage" hint="Checkpoints and recovery files use this verified destination."><Dropdown value={sourceId} onChange={setSourceId} options={sources.map(item => ({ value: idOf(item), label: str(item.name), description: str(item.bucket) }))} /></Field>
            </div>
            {!setupReady && <div className="vela-notice vela-notice-error" role="alert"><strong>Setup needs attention</strong><span>Open Terminal and run <code>{SETUP_COMMAND}</code>, then use Settings for storage and resolve the remaining checks.</span><div className="actions"><button onClick={() => void navigator.clipboard.writeText(SETUP_COMMAND).then(() => setSetupCopied(true)).catch(() => setError("Clipboard access was denied. Select and copy the setup command manually."))}><Clipboard size={14} />{setupCopied ? "Copied" : "Copy setup command"}</button><a className="button vela-button" href={`#/settings${routeQuery({})}`}>Open settings</a></div></div>}
          </>}
        </Panel>
        {error && <div className="vela-notice vela-notice-error" role="alert"><strong>Launch not created</strong><span>{error}</span></div>}
        <div className="training-submit-rail"><div><ShieldCheck size={20} /><span><strong>No provider work starts yet.</strong><small>Creation prepares the export and readiness checks. An agent starts compute only after preflight passes.</small></span></div><button className="vela-button vela-button-primary" disabled={!canSubmit} onClick={() => void createLaunch()}>{busy ? "Preparing…" : "Prepare training run"}</button></div>
      </div>
      <aside className="training-wizard-aside"><strong>What happens next</strong><ol><li>ModelFiche exports this exact version.</li><li>Preflight verifies signing, storage, and data.</li><li>An agent generates a self-contained trainer packet.</li><li>The trainer uploads stable checkpoints through outbound S3.</li><li>ModelFiche imports them into the linked project.</li></ol><p>No tunnel or inbound trainer connection is required. Nothing billable is queued from this screen.</p></aside>
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
  const command = useMemo(() => `${PACKAGED_CLI} --json training run prepare ${id} --output ./modelfiche-training-${id.slice(0, 8)}`, [id]);
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
      <div className={`training-readiness-hero ${ready ? "is-ready" : "is-blocked"}`}><div>{ready ? <ShieldCheck size={28} /> : <TriangleAlert size={28} />}<span><small>Launch readiness</small><strong>{ready ? "Ready for the trainer" : str(preflight.data.phase, "Preparing")}</strong><p>{ready ? "Data, identity, and outbound storage checks passed. No tunnel is required." : str((preflight.data.next_action as Row | undefined)?.label, "ModelFiche is preparing this run.")}</p></span></div><Status value={ready ? "ready" : "blocked"} /></div>
      <Panel title="Preflight"><CheckList checks={listOf<Row>(preflight.data.checks)} /><button className="vela-button" onClick={() => void Promise.all([launch.reload(), preflight.reload()])}>Check again</button></Panel>
      <Panel title="Agent handoff">
        <p className="training-intro">The agent runs one command on this Mac. It verifies preflight again, signs the run-scoped token locally, and creates a checksummed packet without exposing the private key to ModelFiche.</p>
        <div className="training-command"><code>{command}</code><button aria-label="Copy prepare command" onClick={() => void copy(command, "command")}><Clipboard size={16} />{copied === "command" ? "Copied" : "Copy"}</button></div>
        <div className="actions"><a className={`button vela-button ${ready ? "" : "is-disabled"}`} aria-disabled={!ready} href={ready ? `/api/training-launches/${id}/manifest` : undefined}><Download size={15} />Download redacted manifest</a></div>
        {!ready && <small>Packet generation remains locked until every required check passes.</small>}
      </Panel>
      <Panel title="Trainer transport"><dl className="details"><dt>Transport</dt><dd>Outbound object storage</dd><dt>Storage prefix</dt><dd>{str(((launch.data?.manifest as Row | undefined)?.backup as Row | undefined)?.destination && ((((launch.data?.manifest as Row).backup as Row).destination as Row).prefix))}</dd><dt>Run ID</dt><dd>{str(launch.data?.run_id)}</dd></dl><p className="training-intro">The generated packet contains the dataset and sync helper. The trainer needs S3 credentials for the named environment variables, but ModelFiche does not need an inbound tunnel.</p></Panel>
    </>}
  </Page>;
}
