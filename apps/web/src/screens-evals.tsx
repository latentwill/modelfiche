import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  ExternalLink,
  Grid3X3,
  LoaderCircle,
  Star,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource, pollWhileActive } from "./hooks";
import { DataTable, Dropdown, Field, Form, Notice, Page, Panel, Status } from "./ui";
import { galleryOpenHref } from "./screens-assets";
import type { JsonObject } from "./experiment-plan";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const PROGRESS_POLL = { pollInterval: 1000, pollWhile: pollWhileActive };
const OUTPUTS_POLL = { pollInterval: 1000 };


export function EvalsScreen({ projectId = "" }: { projectId?: string; initialModelVersionId?: string }) {
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [project, setProject] = useState(projectId);
  const [runId, setRunId] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState("human_review");
  const [params, setParams] = useState("{}");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const attemptKeys = useRef({ create: crypto.randomUUID(), queue: crypto.randomUUID() });
  const grids = useResource<unknown>(project ? `/api/projects/${encodeURIComponent(project)}/grids` : null);
  const assessments = useResource<unknown>(project ? `/api/projects/${encodeURIComponent(project)}/evals` : null);
  const runRows = useMemo(() => rows(grids.data).flatMap(grid => {
    const nested = rows(grid.runs);
    if (nested.length) return nested.map(run => ({ ...run, grid_id: idOf(grid), grid_name: str(grid.name, "Grid") }));
    return str(grid.run_id ?? grid.generation_run_id, "") ? [grid] : [];
  }).filter(run => Boolean(str(run.run_id ?? run.generation_run_id, "")) && /completed|succeeded/i.test(str(run.status, ""))), [grids.data]);
  const assessmentRows = rows(assessments.data);
  useEffect(() => {
    if (projectId) return;
    setProject("");
  }, [projectId]);
  async function create() {
    if (!project || !runId || !name.trim() || busy) return;
    let parameters: JsonObject;
    try {
      const parsed = JSON.parse(params || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Parameters must be a JSON object.");
      parameters = parsed as JsonObject;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Parameters must be valid JSON.");
      return;
    }
    setBusy(true); setError(""); setMessage("");
    try {
      const created = await api<Row>(`/api/projects/${encodeURIComponent(project)}/evals`, jsonBody({ name: name.trim(), idempotency_key: attemptKeys.current.create, run_id: runId, assessment: { kind, parameters } }));
      const evaluated = await api<Row>(`/api/projects/${encodeURIComponent(project)}/evals/${encodeURIComponent(idOf(created))}/run`, jsonBody({ idempotency_key: attemptKeys.current.queue }));
      setMessage(`Assessment ${idOf(evaluated) || idOf(created)} is ready for review.`);
      attemptKeys.current = { create: crypto.randomUUID(), queue: crypto.randomUUID() };
      await assessments.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  return <Page title="Eval assessment" subtitle="Assess a completed Grid generation run without creating a second generation plan.">
    <Notice error={projects.error || grids.error || assessments.error || error} loading={projects.loading || grids.loading || assessments.loading} />
    <Panel title="Project context"><Field label="Project"><Dropdown aria-label="Project" value={project} options={[{ value: "", label: "Select…" }, ...rows(projects.data).map(row => ({ value: idOf(row), label: str(row.title ?? row.name) }))]} onChange={value => { setProject(value); setRunId(""); setMessage(""); setError(""); attemptKeys.current = { create: crypto.randomUUID(), queue: crypto.randomUUID() }; }} /></Field><p className="vela-muted">Eval is project-scoped. It references an existing Grid generation run and never mutates its plan.</p></Panel>
    <Panel title="Completed Grid generation run"><Field label="Completed run"><Dropdown aria-label="Completed generation run" value={runId} disabled={!project} options={[{ value: "", label: runRows.length ? "Select a completed run…" : "No completed Grid runs" }, ...runRows.map(run => ({ value: str(run.run_id ?? run.generation_run_id), label: `${str(run.grid_name, "Grid")} · ${str(run.run_id ?? run.generation_run_id)}` }))]} onChange={setRunId} /></Field>{selectedRun && <dl className="details"><dt>Grid</dt><dd>{str(selectedRun.grid_name, "Grid")}</dd><dt>Run</dt><dd>{str(selectedRun.run_id ?? selectedRun.generation_run_id)}</dd><dt>Status</dt><dd><Status value={selectedRun.status} /></dd></dl>}</Panel>
    <Panel title="Assessment"><div className="form-grid"><Field label="Name"><input aria-label="Assessment name" value={name} onChange={event => setName(event.target.value)} /></Field><Field label="Assessment kind"><Dropdown aria-label="Assessment kind" value={kind} options={[{ value: "human_review", label: "Human review" }, { value: "image_quality", label: "Image quality" }]} onChange={setKind} /></Field></div><Field label="Parameters"><textarea aria-label="Assessment parameters JSON" value={params} onChange={event => setParams(event.target.value)} /></Field><button type="button" className="vela-button vela-button-primary" disabled={!project || !runId || !name.trim() || busy} onClick={() => void create()}>{busy ? <><LoaderCircle className="vela-spin" size={15} /> Creating…</> : "Create assessment"}</button>{message && <div className="vela-notice" role="status">{message}</div>}</Panel>
    <Panel title="Project assessments"><Notice empty={!assessments.loading && !assessmentRows.length} emptyText="No assessments yet" emptyHint="Create an assessment from a completed Grid run." />{!!assessmentRows.length && <DataTable rows={assessmentRows} columns={[["name", "Name"], ["run_id", "Generation run"], ["assessment", "Assessment"], ["status", "Status"]]} onRow={row => { location.hash = `eval/${idOf(row)}?project=${encodeURIComponent(project)}`; }} />}</Panel>
  </Page>;
}
function EvalRunStrip({ runs }: { runs: Row[] }) {
  return <div className="eval-run-strip">{runs.map(run => <a href={`#/eval/${idOf(run)}`} key={idOf(run)}><div className="comparison-image">{rows(run.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} alt="Eval output preview" loading="lazy" />)}</div><strong>{str(run.name, "Eval run")}</strong><span>{str(run.model_version_name, "Checkpoint")} · {str(run.prompt_count, "0")} prompts · {str(run.output_count, "0")} outputs</span><Status value={run.status} /></a>)}</div>;
}


type InlinePrompt = { id: string; text: string };
function newInlinePrompt(text: string): InlinePrompt { return { id: crypto.randomUUID(), text }; }
export function moveItem<T>(items: T[], from: number, to: number): T[] { const next = [...items]; const [item] = next.splice(from, 1); next.splice(to, 0, item); return next; }


export function EvalScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/eval-runs/${id}`, PROGRESS_POLL);
  const status = str(resource.data?.status, "");
  const terminal = ["succeeded", "failed", "canceled"].includes(status.toLowerCase());
  const outputsResource = useResource<unknown>(`/api/eval-runs/${id}/outputs`, terminal ? {} : OUTPUTS_POLL);
  const outputs = rows(outputsResource.data).length ? rows(outputsResource.data) : rows(resource.data?.outputs);
  const grouped = useMemo(() => groupBy(outputs, output => str(output.prompt_id, "outputs")), [outputs]);
  const promptIds = useMemo(() => Object.keys(grouped), [grouped]);
  const [selected, setSelected] = useState<string[]>([]);
  const [outputPages, setOutputPages] = useState<Record<string, number>>({});
  const [activeReviewId, setActiveReviewId] = useState("");
  const initializedSelection = useRef(false);
  useEffect(() => {
    if (!initializedSelection.current && promptIds.length) {
      initializedSelection.current = true;
      setSelected(promptIds.slice(0, 4));
    }
  }, [promptIds]);
  const shown = Object.entries(grouped).filter(([key]) => selected.includes(key)).slice(0, 4);
  const projectId = str(resource.data?.project_id, "");
  const returnHref = `#/evals${projectId ? `?project=${projectId}` : ""}`;
  const cancelDisabledReason = resource.loading
    ? "Eval data is still loading"
    : resource.error
      ? "Eval data failed to load"
      : !resource.data?.id
        ? "Eval data is unavailable"
        : terminal
          ? `Eval is already ${status}`
          : undefined;

  return (
    <div className="comparison-viewer vela-route-stage">
      <header className="viewer-topbar">
        <div>
          <h1>{str(resource.data?.name, "Eval viewer")}</h1>
          {resource.data && <span>{str(resource.data.plan_digest ?? resource.data.digest, "Inline plan")} · {str(resource.data.model_name, "Model")} / {str(resource.data.model_version_name, "Checkpoint")}</span>}
        </div>
        <div className="viewer-actions">
          <a className="vela-button" href={returnHref}><ArrowLeft size={15} /> Project Evals</a>
          <a className="vela-button" href={`#/transfers?mode=export&eval=${id}&project=${projectId}`}>Export</a>
          <AsyncAction
            label="Cancel"
            successLabel="Canceled"
            icon={<X size={15} />}
            disabled={Boolean(cancelDisabledReason)}
            disabledReason={cancelDisabledReason}
            onRun={async () => { await api(`/api/eval-runs/${id}/cancel`, jsonBody({})); await resource.reload(); }}
          />
          <a className="vela-button" href={returnHref}>Close</a>
        </div>
      </header>
      <main className="vela-main">
        <Notice error={resource.error || outputsResource.error} loading={resource.loading || outputsResource.loading} empty={!resource.loading && !outputsResource.loading && !outputs.length} emptyText="No Eval outputs yet" emptyHint={terminal ? "This Eval completed without reviewable outputs." : "Outputs appear here as provider work completes."} />
        {!!outputs.length && <>
          <div className="comparison-toolbar inline">
            <Status value={resource.data?.status} />
            <div className="chiplets" aria-label="Prompts shown in comparison">
              {promptIds.map(key => {
                const isSelected = selected.includes(key);
                const label = promptLabel(grouped[key][0], key);
                return (
                  <button
                    key={key}
                    type="button"
                    className={isSelected ? "active" : ""}
                    aria-pressed={isSelected}
                    title={!isSelected && selected.length >= 4 ? "A maximum of four prompt groups can be compared" : label}
                    onClick={() => setSelected(toggleLimited(selected, key, 4))}
                  >{truncate(label, 48)}</button>
                );
              })}
            </div>
            <span>{selected.length} selected · max 4 groups · 6 outputs per group page</span>
          </div>
          <div className="comparison-columns embedded eval-review-columns">
            {shown.map(([promptId, promptOutputs]) => {
              const pages = Math.max(1, Math.ceil(promptOutputs.length / 6));
              const page = Math.min(outputPages[promptId] ?? 0, pages - 1);
              const visibleOutputs = promptOutputs.slice(page * 6, page * 6 + 6);
              return <section className="comparison-column" key={promptId}>
                <header><strong>{promptLabel(promptOutputs[0], promptId)}</strong><span>{promptOutputs.length} images · page {page + 1}/{pages}</span></header>
                <p className="prompt-copy">{promptCopy(promptOutputs[0])}</p>
                <GroupComment subjectType="eval_run" subjectId={id} prefix={`Prompt ${promptId}`} />
                {visibleOutputs.map(output => {
                  const outputId = idOf(output);
                  const reviewing = activeReviewId === outputId;
                  return <article className="comparison-image eval-review-output" key={outputId}>
                    <a href={galleryOpenHref({ ...output, project_id: projectId, origin_type: "EVAL" })}><AssetImage assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} loading="lazy" alt={promptCopy(output) || "Eval output"} /></a>
                    <div className="eval-output-summary"><small>Seed {str(output.seed, "not recorded")}</small><button type="button" className="vela-button" aria-expanded={reviewing} onClick={() => setActiveReviewId(reviewing ? "" : outputId)}>{reviewing ? "Close review" : "Review output"}</button></div>
                    {reviewing && <OutputReviewControl output={output} />}
                  </article>;
                })}
                {pages > 1 && <div className="eval-output-pagination"><button type="button" className="vela-button" disabled={page === 0} onClick={() => setOutputPages(current => ({ ...current, [promptId]: page - 1 }))}>Previous 6</button><button type="button" className="vela-button" disabled={page >= pages - 1} onClick={() => setOutputPages(current => ({ ...current, [promptId]: page + 1 }))}>Next 6</button></div>}
              </section>;
            })}
          </div>
        </>}
      </main>
    </div>
  );
}

export function EvalComparisonScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/generation-queue/${id}/eval-results`, PROGRESS_POLL);
  const runs = rows(resource.data?.runs);
  const request = resource.data?.request && typeof resource.data.request === "object" ? resource.data.request as Row : {};
  const orderedPrompts = rows(request.prompts);
  const [queryText, setQueryText] = useState("");
  const [visibleModels, setVisibleModels] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  useEffect(() => { if (!visibleModels.length && runs.length) setVisibleModels(runs.map(run => str(run.model_version_id))); }, [resource.data]);
  useEffect(() => { setPage(0); }, [queryText]);
  const shownRuns = runs.filter(run => visibleModels.includes(str(run.model_version_id)));
  const filteredPrompts = orderedPrompts.filter(prompt => !queryText || str(prompt.prompt).toLowerCase().includes(queryText.toLowerCase()));
  const pageCount = Math.max(1, Math.ceil(filteredPrompts.length / 12));
  const safePage = Math.min(page, pageCount - 1);
  const shownPrompts = filteredPrompts.slice(safePage * 12, safePage * 12 + 12);
  const context = request.context && typeof request.context === "object" ? request.context as Row : {};
  const projectId = str(resource.data?.project_id ?? context.projectId ?? context.project_id, "");
  const promptSetId = str((request.prompt_set as Row | undefined)?.id, "");
  const returnParams = new URLSearchParams();
  if (projectId) returnParams.set("project", projectId);
  if (promptSetId) returnParams.set("prompt_set", promptSetId);
  return <Page title="Eval comparison" subtitle="Matching prompts are aligned across selected model checkpoints." actions={<a className="vela-button" href={`#/evals${returnParams.size ? `?${returnParams}` : ""}`}><ArrowLeft size={15} /> Project Evals</a>}>
    <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !runs.length} emptyText="No comparison results yet" emptyHint="This comparison updates while queued checkpoint work completes." />
    {!!runs.length && <>
      <Panel title="Comparison filters"><div className="form-grid"><Field label="Prompt contains"><input value={queryText} onChange={event => setQueryText(event.target.value)} /></Field><Field label="Model checkpoints"><select multiple value={visibleModels} onChange={event => setVisibleModels(Array.from(event.target.selectedOptions, option => option.value))}>{runs.map(run => <option key={str(run.model_version_id)} value={str(run.model_version_id)}>{str(run.model_name)} · {str(run.model_version_name)}</option>)}</select></Field></div></Panel>
      <Panel title="Results"><div className="eval-comparison-range"><span>Showing {filteredPrompts.length ? safePage * 12 + 1 : 0}–{Math.min((safePage + 1) * 12, filteredPrompts.length)} of {filteredPrompts.length} prompts</span><div><button type="button" className="vela-button" disabled={safePage === 0} onClick={() => setPage(value => Math.max(0, value - 1))}>Previous 12</button><button type="button" className="vela-button" disabled={safePage >= pageCount - 1} onClick={() => setPage(value => Math.min(pageCount - 1, value + 1))}>Next 12</button></div></div><div className="eval-comparison-matrix" role="region" aria-label="Eval comparison table. Scroll within this region to inspect checkpoints." tabIndex={0} style={{ "--eval-model-count": Math.max(1, shownRuns.length) } as CSSProperties}>
        <div className="eval-comparison-header"><strong>Prompt</strong>{shownRuns.map(run => <strong key={str(run.model_version_id)}>{str(run.model_name)}<small>{str(run.model_version_name)} · step {str(run.checkpoint_step, "—")}</small></strong>)}</div>
        {shownPrompts.map((prompt, promptIndex) => <div className="eval-comparison-row" key={str(prompt.prompt_id, String(promptIndex))}><p>{str(prompt.prompt)}</p>{shownRuns.map(run => { const output = rows(run.outputs).find(item => str(item.prompt_id) === str(prompt.prompt_id)); return <div key={str(run.model_version_id)} data-checkpoint={`${str(run.model_name)} · ${str(run.model_version_name)}`}>{output ? <a href={galleryOpenHref({ ...output, project_id: projectId, origin_type: "EVAL", eval_run_id: run.eval_run_id })}><AssetImage assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} alt={str(prompt.prompt)} loading="lazy" /></a> : <div className="vela-empty">{str(run.status) === "completed" ? "No output" : str(run.status)}</div>}</div>; })}</div>)}
      </div></Panel>
    </>}
  </Page>;
}

export function OutputReviewControl({ output }: { output: Row }) {
  const [rating, setRating] = useState(Number(output.rating) || 0);
  const [decision, setDecision] = useState(str(output.decision, ""));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const save = async (nextRating: number, nextDecision: string) => {
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const saved = await api<Row>("/api/reviews", jsonBody({ subject_type: "eval_output", subject_id: idOf(output), rating: nextRating || null, decision: nextDecision || null }));
      setRating(nextRating);
      setDecision(nextDecision);
      setMessage(`Saved${saved.profile_name ? ` by ${str(saved.profile_name)}` : ""}.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  return <div className="eval-output-review" aria-label="Output rating and production decision">
    <RovingRadioGroup
      label="Rating"
      value={rating ? String(rating) : ""}
      disabled={saving}
      options={[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: `${value} star${value === 1 ? "" : "s"}`, content: <><Star size={13} fill={rating >= value ? "currentColor" : "none"} /><span>{value}</span></> }))}
      onChange={value => void save(Number(value), decision)}
    />
    <RovingRadioGroup
      label="Production decision"
      value={decision}
      disabled={saving}
      options={[
        { value: "candidate", label: "Candidate" },
        { value: "approved", label: "Approved" },
        { value: "hold", label: "Hold" },
        { value: "reject", label: "Reject" },
      ]}
      onChange={value => void save(rating, value)}
    />
    {saving && <span role="status"><LoaderCircle className="vela-spin" size={13} /> Saving review…</span>}
    {message && <span role="status"><Check size={13} /> {message}</span>}
    {error && <small role="alert">{error}</small>}
  </div>;
}

function RovingRadioGroup({ label, value, options, disabled, onChange }: { label: string; value: string; options: Array<{ value: string; label: string; content?: ReactNode }>; disabled: boolean; onChange: (value: string) => void }) {
  const activeIndex = Math.max(0, options.findIndex(option => option.value === value));
  return <div className="eval-roving-group" role="radiogroup" aria-label={label}>{options.map((option, index) => <button
    key={option.value}
    type="button"
    role="radio"
    aria-checked={option.value === value}
    aria-label={option.label}
    tabIndex={index === activeIndex ? 0 : -1}
    disabled={disabled}
    onClick={() => onChange(option.value)}
    onKeyDown={event => {
      let next = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % options.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + options.length) % options.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = options.length - 1;
      else return;
      event.preventDefault();
      const buttons = event.currentTarget.parentElement?.querySelectorAll<HTMLElement>('[role="radio"]');
      buttons?.[next]?.focus();
      onChange(options[next].value);
    }}
  >{option.content ?? option.label}</button>)}</div>;
}





export function ReviewsScreen({ subject = "", projectId = "" }: { subject?: string; projectId?: string }) {
  const [selectedSubject, setSelectedSubject] = useState(subject);
  const [subjectType, subjectId] = splitSubject(selectedSubject.trim());
  const hasTypedSubject = Boolean(subjectType && subjectId);
  const reviews = useResource<unknown>(`/api/reviews${query({ subject_type: subjectType || undefined, subject_id: subjectId || undefined, project_id: projectId || undefined })}`);
  const comments = useResource<unknown>(hasTypedSubject ? `/api/comments${query({ subject_type: subjectType, subject_id: subjectId })}` : null);

  return (
    <Page title="Reviews and comments" subtitle="Audit decisions and discussion by typed object reference.">
      <Panel title="Subject">
        <Field label="Typed subject" hint="Use type:id, for example asset:3f2... or eval_run:9ad...">
          <input value={selectedSubject} onChange={event => setSelectedSubject(event.target.value)} placeholder="asset:uuid" />
        </Field>
        {selectedSubject && !hasTypedSubject && <div className="vela-notice vela-notice-error" role="alert"><strong>Subject type required</strong><span>Enter the reference as type:id.</span></div>}
      </Panel>
      {hasTypedSubject && (
        <Panel title="Decision">
          <Form submit="Save review" onSubmit={async form => { await api("/api/reviews", jsonBody({ subject_type: subjectType, subject_id: subjectId, rating: Number(form.get("rating")), decision: form.get("decision") })); await reviews.reload(); }}>
            <div className="form-grid">
              <Field label="Rating">
                <Dropdown
                  aria-label="Rating"
                  name="rating"
                  defaultValue="3"
                  options={[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: String(value) }))}
                />
              </Field>
              <Field label="Decision">
                <Dropdown
                  aria-label="Decision"
                  name="decision"
                  defaultValue="candidate"
                  options={["candidate", "approved", "hold", "reject"].map(value => ({ value, label: value }))}
                />
              </Field>
            </div>
          </Form>
        </Panel>
      )}
      <Panel title="Reviews">
        <Notice error={reviews.error} loading={reviews.loading} empty={!reviews.loading && !rows(reviews.data).length} />
        <DataTable rows={rows(reviews.data)} columns={[["subject", "Subject"], ["rating", "Rating"], ["decision", "Decision"], ["profile_name", "Profile"]]} />
      </Panel>
      {hasTypedSubject && (
        <Panel title="Comments">
          <Notice error={comments.error} loading={comments.loading} empty={!comments.loading && !rows(comments.data).length} />
          <div className="comments">
            {rows(comments.data).map(comment => <article key={idOf(comment)}><strong>{str(comment.profile_name, "Local profile")}</strong><p>{str(comment.body)}</p></article>)}
          </div>
          <Form submit="Add comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: subjectType, subject_id: subjectId, body: String(form.get("body")) })); await comments.reload(); }}>
            <Field label="Comment"><textarea name="body" required /></Field>
          </Form>
        </Panel>
      )}
    </Page>
  );
}

function AsyncAction({ label, workingLabel = "Working...", successLabel = "Done", icon, disabled = false, disabledReason, onRun }: {
  label: string;
  workingLabel?: string;
  successLabel?: string;
  icon?: ReactNode;
  disabled?: boolean;
  disabledReason?: string;
  onRun: () => Promise<void>;
}) {
  const [state, setState] = useState<"idle" | "working" | "success">("idle");
  const [error, setError] = useState("");
  return (
    <div>
      <button
        className="vela-button"
        type="button"
        disabled={disabled || state === "working"}
        title={disabled ? disabledReason : undefined}
        onClick={async () => {
          setError("");
          setState("working");
          try {
            await onRun();
            setState("success");
            window.setTimeout(() => setState("idle"), 1600);
          } catch (reason) {
            setState("idle");
            setError(reason instanceof Error ? reason.message : String(reason));
          }
        }}
      >
        {state === "working" ? <LoaderCircle className="vela-spin" size={15} /> : state === "success" ? <Check size={15} /> : icon}
        {state === "working" ? workingLabel : state === "success" ? successLabel : label}
      </button>
      {error && <small role="alert">{error}</small>}
    </div>
  );
}


function commonEndpointSchema(schemas: Row[]): Row | undefined {
  if (!schemas.length) return undefined;
  const requestFields = schemas.map(schema => new Set(Array.isArray(schema.request_fields) ? schema.request_fields.map(String) : []));
  const common = Array.from(requestFields[0]).filter(field => requestFields.every(fields => fields.has(field)));
  const first = schemas[0];
  const defaults = (first.defaults ?? {}) as Row;
  const fields = (first.fields ?? {}) as Record<string, Row>;
  const sharedFields = Object.fromEntries(common.flatMap(field => {
    if (!fields[field]) return [];
    const definitions = schemas.map(schema => ((schema.fields ?? {}) as Record<string, Row>)[field]).filter((value): value is Row => Boolean(value));
    const merged = { ...fields[field] };
    const optionSets = definitions.map(definition => Array.isArray(definition.options) ? definition.options.map(String) : []);
    if (optionSets.length === definitions.length && optionSets.every(options => options.length)) {
      merged.options = optionSets[0].filter(option => optionSets.every(options => options.includes(option)));
    }
    if (definitions.every(definition => ["integer", "number"].includes(str(definition.type, "")))) {
      const minimums = definitions.map(definition => definition.min).filter(value => value !== undefined).map(Number);
      const maximums = definitions.map(definition => definition.max).filter(value => value !== undefined).map(Number);
      const steps = definitions.map(definition => definition.step).filter(value => value !== undefined).map(Number);
      if (minimums.length) merged.min = Math.max(...minimums);
      if (maximums.length) merged.max = Math.min(...maximums);
      if (steps.length) merged.step = Math.max(...steps);
    }
    if (definitions.every(definition => definition.custom && typeof definition.custom === "object")) {
      const custom = definitions.map(definition => definition.custom as Row);
      merged.custom = {
        min: Math.max(...custom.map(value => Number(value.min))),
        max: Math.min(...custom.map(value => Number(value.max))),
        step: Math.max(...custom.map(value => Number(value.step))),
      };
    }
    return [[field, merged]];
  }));
  const loraDefinitions = schemas.flatMap(schema => {
    const grid = schema.grid && typeof schema.grid === "object" ? schema.grid as Row : {};
    const axisFields = grid.axis_fields && typeof grid.axis_fields === "object" ? grid.axis_fields as Record<string, Row> : {};
    return axisFields.lora_scale ? [axisFields.lora_scale] : [];
  });
  const loraScale = loraDefinitions.length === schemas.length ? {
    ...loraDefinitions[0],
    min: Math.max(...loraDefinitions.map(field => Number(field.min))),
    max: Math.min(...loraDefinitions.map(field => Number(field.max))),
    step: Math.max(...loraDefinitions.map(field => Number(field.step))),
  } : undefined;
  const firstGrid = first.grid && typeof first.grid === "object" ? first.grid as Row : {};
  const firstGridAxes = firstGrid.axis_fields && typeof firstGrid.axis_fields === "object" ? firstGrid.axis_fields as Record<string, Row> : {};
  return {
    ...first,
    request_fields: common,
    defaults: Object.fromEntries(common.filter(field => defaults[field] !== undefined).map(field => [field, defaults[field]])),
    fields: sharedFields,
    grid: { ...firstGrid, axis_fields: { ...firstGridAxes, lora_scale: loraScale } },
  };
}

function parsePrompts(value: string): unknown[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [parsed];
  } catch {
    return value.split("\n").map(line => line.trim()).filter(Boolean);
  }
}


function groupBy(items: Row[], key: (item: Row) => string) {
  return items.reduce<Record<string, Row[]>>((result, item) => {
    (result[key(item)] ??= []).push(item);
    return result;
  }, {});
}

function toggleLimited(values: string[], value: string, max: number) {
  return values.includes(value) ? values.filter(item => item !== value) : values.length < max ? [...values, value] : values;
}

function splitSubject(subject: string): [string, string] {
  const index = subject.indexOf(":");
  return index > 0 ? [subject.slice(0, index), subject.slice(index + 1)] : ["", subject];
}

function unique<T>(values: T[]) { return [...new Set(values)]; }
function truncate(value: string, limit: number) { return value.length > limit ? `${value.slice(0, limit - 3)}...` : value; }

function promptCopy(output?: Row) { return str(output?.prompt_text ?? output?.prompt, ""); }
function promptLabel(output: Row | undefined, fallback: string) { return str(output?.prompt_name ?? output?.prompt_text ?? output?.prompt, `Prompt ${fallback.slice(0, 6)}`); }

function GroupComment({ subjectType, subjectId, prefix }: { subjectType: string; subjectId: string; prefix: string }) {
  return (
    <Form submit="Add group comment" onSubmit={async form => { await api("/api/comments", jsonBody({ subject_type: subjectType, subject_id: subjectId, body: `[${prefix}] ${String(form.get("body"))}` })); }}>
      <Field label="Group comment"><textarea name="body" required /></Field>
    </Form>
  );
}
