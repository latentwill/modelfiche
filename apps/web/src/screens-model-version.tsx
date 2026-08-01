import { useEffect, useState } from "react";
import { Archive, Check, ExternalLink } from "lucide-react";
import { api, idOf, jsonBody, listOf, query, routeQuery, str } from "./api";
import { useResource } from "./hooks";
import { LineageList } from "./lineage";
import { Dropdown, Field, Json, Notice, Page, Panel, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);

function formatBytes(value: unknown) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) { amount /= 1024; unit = units[index]; }
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${unit}`;
}

export function ModelVersionScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/model-versions/${id}`);
  const reviews = useResource<unknown>(`/api/reviews${query({ subject_type: "model_version", subject_id: id })}`);
  const lineage = useResource<unknown>(`/api/lineage${query({ subject_type: "model_version", subject_id: id, depth: 3 })}`);
  const currentReview = rows(reviews.data)[0] ?? null;
  const [decision, setDecision] = useState("candidate");
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [decisionError, setDecisionError] = useState("");
  const [decisionMessage, setDecisionMessage] = useState("");
  const [lifecycleReviewed, setLifecycleReviewed] = useState(false);
  const [lifecycleBusy, setLifecycleBusy] = useState("");
  const [lifecycleError, setLifecycleError] = useState("");
  const [lifecycleMessage, setLifecycleMessage] = useState("");
  const artifact = resource.data?.artifact && typeof resource.data.artifact === "object" ? resource.data.artifact as Row : {};
  const readiness = resource.data?.readiness_summary && typeof resource.data.readiness_summary === "object" ? resource.data.readiness_summary as Row : {};
  const local = readiness.local && typeof readiness.local === "object" ? readiness.local as Row : {};
  const fal = readiness.fal && typeof readiness.fal === "object" ? readiness.fal as Row : {};
  const dataset = resource.data?.dataset && typeof resource.data.dataset === "object" ? resource.data.dataset as Row : null;
  const storage = rows(artifact.storage);
  const projectId = str(resource.data?.project_id ?? resource.data?.model_project_id ?? dataset?.project_id, "");
  useEffect(() => {
    if (currentReview?.decision) setDecision(str(currentReview.decision));
  }, [currentReview?.id, currentReview?.decision]);

  async function saveDecision() {
    setDecisionBusy(true); setDecisionError(""); setDecisionMessage("");
    try {
      const saved = await api<Row>("/api/reviews", jsonBody({ subject_type: "model_version", subject_id: id, decision, rating: currentReview?.rating ?? null }));
      setDecisionMessage(`${str(saved.decision, decision)} saved by ${str(saved.profile_name, "current operator")}.`);
      await reviews.reload();
    } catch (reason) { setDecisionError(errorText(reason)); } finally { setDecisionBusy(false); }
  }

  async function changeLifecycle(action: "approve" | "archive") {
    if (!lifecycleReviewed) return;
    setLifecycleBusy(action); setLifecycleError(""); setLifecycleMessage("");
    try {
      await api(`/api/model-versions/${id}/${action}`, jsonBody({}));
      setLifecycleMessage(action === "approve" ? "Registration lifecycle set to approved." : "Registration archived. Provider artifacts were not deleted.");
      await resource.reload();
    } catch (reason) { setLifecycleError(errorText(reason)); } finally { setLifecycleBusy(""); }
  }

  if (!resource.data && resource.loading) return <Page title="Model version" subtitle="Loading registered version"><section aria-busy="true"><Notice loading /></section></Page>;
  if (!resource.data && resource.error) return <Page title="Model version unavailable" subtitle={`Version ${id}`}><Notice error={resource.error} /><div className="actions"><a className="button vela-button" href={`#/models${routeQuery({ project: projectId })}`}>Back to models</a><button onClick={() => void resource.reload()}>Retry</button></div></Page>;
  return <Page title={str(resource.data?.name, "Registered model version")} subtitle={`${str(resource.data?.model_name, "Model")} · Registered version`} actions={<>
    {resource.data?.model_id && <a className="button vela-button" href={`#/model/${str(resource.data.model_id)}${routeQuery({ project: projectId })}`}>Model</a>}
    {resource.data?.checkpoint_id && <a className="button vela-button" href={`#/checkpoint/${str(resource.data.checkpoint_id)}${routeQuery({ project: projectId })}`}>Checkpoint artifact</a>}
    {resource.data?.checkpoint_run_id && <a className="button vela-button" href={`#/run/${str(resource.data.checkpoint_run_id)}${routeQuery({ project: projectId })}`}>Source run</a>}
  </>}>
    <Panel title="Artifact contract"><dl className="details"><dt>Type</dt><dd>{str(resource.data?.artifact_type, "lora").replaceAll("_", " ")}</dd><dt>Format</dt><dd>{str(resource.data?.artifact_format, "Not recorded")}</dd><dt>Method</dt><dd>{str(resource.data?.method, "Not recorded")}</dd></dl><details className="config-disclosure"><summary>Compatibility and tensor metadata</summary><Json value={resource.data?.compatibility ?? {}} /></details></Panel>
    <Panel title="Registration record"><dl className="details"><dt>Version ID</dt><dd>{id}</dd><dt>Model</dt><dd>{resource.data?.model_id ? <a href={`#/model/${str(resource.data.model_id)}${routeQuery({ project: projectId })}`}>{str(resource.data.model_name)}</a> : str(resource.data?.model_name)}</dd><dt>Lifecycle</dt><dd><Status value={resource.data?.lifecycle_state} /></dd><dt>Base model</dt><dd>{str(resource.data?.base_model, "Not recorded")}</dd><dt>Trigger words</dt><dd>{Array.isArray(resource.data?.trigger_words) && resource.data.trigger_words.length ? resource.data.trigger_words.join(", ") : "None registered"}</dd><dt>Notes</dt><dd>{str(resource.data?.notes, "No notes")}</dd><dt>Dataset</dt><dd>{dataset?.dataset_id ? <a href={`#/dataset/${str(dataset.dataset_id)}${routeQuery({ project: projectId })}`}>{str(dataset.dataset_name)} · {str(dataset.dataset_version_name, "Version")}</a> : str(dataset?.dataset_name, "Not linked")}</dd><dt>Source run</dt><dd>{resource.data?.checkpoint_run_id ? <a href={`#/run/${str(resource.data.checkpoint_run_id)}${routeQuery({ project: projectId })}`}>{str(resource.data.checkpoint_run_name, "Open source run")}</a> : "Not recorded"}</dd></dl></Panel>
    <div className="model-version-detail-grid">
      <Panel title="Checkpoint artifact"><dl className="details"><dt>Checkpoint</dt><dd>{resource.data?.checkpoint_id ? <a href={`#/checkpoint/${str(resource.data.checkpoint_id)}${routeQuery({ project: projectId })}`}>Step {str(resource.data.checkpoint_step, "not recorded")}</a> : "Unavailable"}</dd><dt>Filename</dt><dd>{str(artifact.filename, "Missing asset record")}</dd><dt>Artifact ID</dt><dd>{str(artifact.asset_id)}</dd><dt>Immutable revision</dt><dd>{str(artifact.checkpoint_revision_id, "No materialized revision")}</dd><dt>SHA-256</dt><dd>{str(artifact.sha256, "Not recorded")}</dd><dt>Size</dt><dd>{formatBytes(storage.find(location => location.size != null)?.size)}</dd></dl><details className="config-disclosure"><summary>Artifact metadata</summary><Json value={artifact.metadata ?? {}} /></details></Panel>
      <Panel title="Readiness"><dl className="details"><dt>Local</dt><dd><Status value={local.status ?? "unknown"} /> {str(local.reason)}</dd><dt>Local path</dt><dd>{str(local.path, "No local copy")}</dd><dt>Verification</dt><dd>{str(local.verification_state, "Not recorded")}</dd><dt>FAL</dt><dd><Status value={fal.status ?? "not registered"} /> {str(fal.reason)}</dd><dt>Endpoint</dt><dd>{str(fal.endpoint_id, "Not registered")}</dd><dt>Provider artifact</dt><dd>{fal.url ? <a href={str(fal.url)} target="_blank" rel="noreferrer"><ExternalLink size={14} /> Open registered artifact</a> : "Not registered"}</dd></dl></Panel>
    </div>
    <Panel title="Production decision"><Notice error={reviews.error || decisionError} loading={reviews.loading} /><div className="production-decision-layout"><Field label="Decision" hint="A production decision is independent of registration lifecycle and rating."><Dropdown aria-label="Production decision" value={decision} onChange={setDecision} options={[{ value: "candidate", label: "Candidate" }, { value: "approved", label: "Approved" }, { value: "hold", label: "Hold" }, { value: "reject", label: "Reject" }]} /></Field><button className="vela-button vela-button-primary" disabled={decisionBusy} onClick={() => void saveDecision()}>{decisionBusy ? "Saving decision..." : "Save decision"}</button></div>{currentReview && <p className="vela-notice" role="status"><strong>Persisted decision</strong><span>{str(currentReview.decision, "No decision")} by {str(currentReview.profile_name, "Unknown operator")} · {new Date(str(currentReview.created_at)).toLocaleString()}</span></p>}{decisionMessage && <p className="vela-notice" role="status" aria-live="polite">{decisionMessage}</p>}</Panel>
    <Panel title="Registration lifecycle"><p>Approving changes the reusable registration state. Archiving hides it from active selection but does not delete the checkpoint or provider artifact.</p><label><input type="checkbox" checked={lifecycleReviewed} onChange={event => setLifecycleReviewed(event.target.checked)} /> I reviewed the checkpoint, readiness, lineage, and downstream impact.</label><div className="actions"><button className="primary" disabled={!lifecycleReviewed || Boolean(lifecycleBusy) || resource.data?.lifecycle_state === "approved"} onClick={() => void changeLifecycle("approve")}><Check size={14} />{lifecycleBusy === "approve" ? "Approving..." : "Approve registration"}</button><button disabled={!lifecycleReviewed || Boolean(lifecycleBusy) || resource.data?.lifecycle_state === "archived"} onClick={() => void changeLifecycle("archive")}><Archive size={14} />{lifecycleBusy === "archive" ? "Archiving..." : "Archive registration"}</button></div>{lifecycleMessage && <p className="vela-notice" role="status" aria-live="polite">{lifecycleMessage}</p>}{lifecycleError && <p className="vela-notice vela-notice-error" role="alert">{lifecycleError}</p>}</Panel>
    <Panel title="Typed lineage"><Notice error={lineage.error} loading={lineage.loading} empty={!lineage.loading && !rows(lineage.data).length} emptyText="No lineage indexed" emptyHint="No source-derived checkpoint, run, or dataset relationships were found." />{!!rows(lineage.data).length && <LineageList value={lineage.data} />}</Panel>
  </Page>;
}
