import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import {
  ArrowLeft,
  Check,
  LoaderCircle,
  Star,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, query, routeQuery, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource, pollWhileActive } from "./hooks";
import { DataTable, Dropdown, Field, Form, Notice, Page, Panel, Status } from "./ui";
import { galleryOpenHref } from "./screens-assets";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const PROGRESS_POLL = { pollInterval: 1000, pollWhile: pollWhileActive };
const OUTPUTS_POLL = { pollInterval: 4000, pollWhile: pollWhileActive };




export function moveItem<T>(items: T[], from: number, to: number): T[] { const next = [...items]; const [item] = next.splice(from, 1); next.splice(to, 0, item); return next; }

function ProjectEvalDetail({ id, projectId, resource }: { id: string; projectId: string; resource: { data: Row | null; loading: boolean; error?: string } }) {
  const assessment = resource.data?.assessment && typeof resource.data.assessment === "object" ? resource.data.assessment as Row : {};
  const reviews = rows(resource.data?.reviews);
  return <div className="comparison-viewer vela-route-stage"><header className="viewer-topbar"><div><h1>{str(resource.data?.name, "Assessment")}</h1><span>{str(resource.data?.status, "Loading")} · project {projectId}</span></div><a className="vela-button" href={`#/grids${routeQuery({ project: projectId })}`}><ArrowLeft size={15} /> Project Grids</a></header><main className="vela-main"><Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !resource.data} emptyText="Assessment unavailable" /><Panel title="Assessment provenance"><dl className="details"><dt>Project</dt><dd>{projectId}</dd><dt>Generation run</dt><dd>{str(resource.data?.run_id, "Unknown")}</dd><dt>Plan</dt><dd>{str(resource.data?.plan_id, "Unknown")}</dd><dt>Plan digest</dt><dd>{str(resource.data?.plan_digest, "Unknown")}</dd><dt>Status</dt><dd>{str(resource.data?.status, "Unknown")}</dd></dl></Panel>{reviews.length ? <Panel title="Reviews"><DataTable rows={reviews} columns={[["reviewer", "Reviewer"], ["decision", "Decision"], ["notes", "Notes"], ["created_at", "Created"]]} /></Panel> : null}</main></div>;
}


export function evalOutputProvenanceLabel(output: Row): { title: string; detail: string; kind: string } {
  const provenance = output.provenance && typeof output.provenance === "object" ? output.provenance as Row : {};
  const provider = output.provider_metadata && typeof output.provider_metadata === "object" ? output.provider_metadata as Row : {};
  const grid = provider.grid_metadata && typeof provider.grid_metadata === "object" ? provider.grid_metadata as Row : {};
  const axisValues = provenance.axis_values && typeof provenance.axis_values === "object" ? provenance.axis_values as Row : {};
  const target = str(axisValues.target, "");
  const modelName = str(provenance.model_name ?? grid.model_name, "");
  const versionName = str(provenance.model_version_name ?? grid.model_version_name, "");
  const step = str(provenance.checkpoint_step ?? grid.checkpoint_step, "");
  const kind = str(provenance.kind, "checkpoint");
  const mergeNotation = str(provenance.merge_notation, "");
  const trainingRun = str(provenance.training_run_name ?? grid.run_name, "");
  const title = target ? `Target · ${target}` : modelName || versionName || "Grid target";
  const detail = kind === "merge" || mergeNotation
    ? ["Merge", mergeNotation || modelName || versionName].filter(Boolean).join(" · ")
    : [modelName || versionName, step ? `step ${step}` : "", trainingRun && trainingRun !== modelName ? `run ${trainingRun}` : ""].filter(Boolean).join(" · ");
  return { title, detail: detail || "Checkpoint metadata unavailable", kind };
}


export function EvalScreen({ id, projectId: routeProjectId = "" }: { id: string; projectId?: string }) {
  const resource = useResource<Row>(routeProjectId ? null : `/api/eval-runs/${id}`, PROGRESS_POLL);
  const assessmentResource = useResource<Row>(routeProjectId ? `/api/projects/${encodeURIComponent(routeProjectId)}/evals/${encodeURIComponent(id)}` : null, PROGRESS_POLL);
  const status = str(resource.data?.status, "");
  const terminal = ["succeeded", "failed", "canceled"].includes(status.toLowerCase());
  const outputsResource = useResource<unknown>(routeProjectId ? null : `/api/eval-runs/${id}/outputs`, OUTPUTS_POLL);
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
  const returnHref = `#/grids${routeQuery({ project: projectId })}`;
  const cancelDisabledReason = resource.loading
    ? "Eval data is still loading"
    : resource.error
      ? "Eval data failed to load"
      : !resource.data?.id
        ? "Eval data is unavailable"
        : terminal
          ? `Eval is already ${status}`
          : undefined;
  if (routeProjectId) return <ProjectEvalDetail id={id} projectId={routeProjectId} resource={assessmentResource} />;

  return (
    <div className="comparison-viewer vela-route-stage">
      <header className="viewer-topbar">
        <div>
          <h1>{str(resource.data?.name, "Eval viewer")}</h1>
          {resource.data && <span>{str(resource.data.plan_digest ?? resource.data.digest, "Inline plan")} · {str(resource.data.model_name, "Model")} / {str(resource.data.model_version_name, "Checkpoint")}</span>}
        </div>
        <div className="viewer-actions">
          <a className="vela-button" href={returnHref}><ArrowLeft size={15} /> Project Grids</a>
          <a className="vela-button" href={`#/transfers${routeQuery({ mode: "export", eval: id, project: projectId })}`}>Export</a>
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
                  const provenance = evalOutputProvenanceLabel(output);
                  return <article className="comparison-image eval-review-output" data-provenance-kind={provenance.kind} key={outputId}>
                    <a href={galleryOpenHref({ ...output, project_id: projectId, origin_type: "EVAL" })}><AssetImage assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} loading="lazy" alt={`${provenance.title} · ${promptCopy(output) || "Eval output"}`} /></a>
                    <div className="eval-output-provenance"><strong>{provenance.title}</strong><small>{provenance.detail}</small></div>
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
  return <Page title="Eval comparison" subtitle="Matching prompts are aligned across selected model checkpoints." actions={<a className="vela-button" href={`#/grids${routeQuery({ project: projectId })}`}><ArrowLeft size={15} /> Project Grids</a>}>
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
  const resetTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => {
    if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current);
  }, []);
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
            resetTimer.current = window.setTimeout(() => setState("idle"), 1600);
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
