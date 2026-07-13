import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  ExternalLink,
  Grid3X3,
  LoaderCircle,
  Play,
  Star,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { DataTable, Field, Form, Notice, Page, Panel, SelectRecords, Status } from "./ui";
import { EndpointFields, GenerationQueue, ModelCheckpointPicker, parametersFromForm } from "./generator";
import { connectedFalGenerationModels, generationModelFromRow } from "./generation";
import { galleryOpenHref } from "./screens-assets";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

export function PromptSetsScreen({ projectId = "", promptSetId = "" }: { projectId?: string; promptSetId?: string }) {
  const resource = useResource<unknown>(`/api/prompt-sets${query({ project_id: projectId })}`);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [selectedId, setSelectedId] = useState(promptSetId);
  const selected = useResource<Row>(selectedId ? `/api/prompt-sets/${selectedId}` : null);
  useEffect(() => { setSelectedId(promptSetId); }, [promptSetId]);

  return (
    <Page
      title="Prompt sets"
      subtitle="Build and version the ordered prompts used by evaluations and checkpoint grids."
      actions={
        <>
          <a className="vela-button" href={`#/evals?project=${projectId}`}>Eval generator</a>
          <a className="vela-button" href={`#/grids?project=${projectId}`}><Grid3X3 size={15} /> Grid generator</a>
        </>
      }
    >
      <Notice error={resource.error || projects.error} loading={resource.loading || projects.loading} />
      <PromptSetBuilder
        projects={rows(projects.data)}
        resource={resource}
        selected={selected}
        selectedId={selectedId}
        onSelect={setSelectedId}
        initialProject={projectId}
      />
    </Page>
  );
}

function PromptSetBuilder({ projects, resource, selected, selectedId, onSelect, initialProject = "" }: {
  projects: Row[];
  resource: ReturnType<typeof useResource<unknown>>;
  selected: ReturnType<typeof useResource<Row>>;
  selectedId: string;
  onSelect: (id: string) => void;
  initialProject?: string;
}) {
  const [find, setFind] = useState("");
  const [replace, setReplace] = useState("");
  const [instruction, setInstruction] = useState("");
  const [promptText, setPromptText] = useState("");
  const [generationStatus, setGenerationStatus] = useState("");
  const [movingIndex, setMovingIndex] = useState<number | null>(null);
  const [moveStatus, setMoveStatus] = useState("");
  const [moveError, setMoveError] = useState("");
  const moveLock = useRef(false);
  const prompts = rows(selected.data?.prompts);
  const moving = movingIndex !== null;
  const reorderPrompt = async (index: number, direction: number) => {
    if (moveLock.current) return;
    moveLock.current = true;
    setMovingIndex(index);
    setMoveStatus("");
    setMoveError("");
    try {
      const next = prompts.map(prompt => str(prompt.text));
      [next[index], next[index + direction]] = [next[index + direction], next[index]];
      const created = await api<Row>(`/api/prompt-sets/${selectedId}/versions`, jsonBody({ prompts: next }));
      await resource.reload();
      onSelect(idOf(created));
      setMoveStatus(created.version ? `Version ${str(created.version)} saved with the new prompt order.` : "A new version was saved with the new prompt order.");
    } catch (reason) {
      setMoveError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      moveLock.current = false;
      setMovingIndex(null);
    }
  };

  return (
    <Panel title="Prompt-set builder">
      <div className="prompt-builder-toolbar">
        <Field label="Saved set">
          <select value={selectedId} onChange={event => onSelect(event.target.value)}>
            <option value="">New prompt set...</option>
            {rows(resource.data).map(set => (
              <option key={idOf(set)} value={idOf(set)}>{str(set.name)} · v{str(set.version, "1")}</option>
            ))}
          </select>
        </Field>
        <Field label="Find"><input value={find} onChange={event => setFind(event.target.value)} /></Field>
        <Field label="Replace"><input value={replace} onChange={event => setReplace(event.target.value)} /></Field>
        <AsyncAction
          label="Replace in new version"
          successLabel="Version created"
          disabled={!selectedId || !find}
          disabledReason={!selectedId ? "Select a saved prompt set first" : "Enter text to find"}
          onRun={async () => {
            const next = prompts.map(prompt => str(prompt.text).replaceAll(find, replace));
            const created = await api<Row>(`/api/prompt-sets/${selectedId}/versions`, jsonBody({ prompts: next }));
            await resource.reload();
            onSelect(idOf(created));
          }}
        />
      </div>

      {selectedId && (
        <div className="prompt-rows">
          <Notice error={selected.error} loading={selected.loading} empty={!selected.loading && !prompts.length} />
          {moveError && <div className="vela-notice vela-notice-error" role="alert"><strong>Order not saved</strong><span>{moveError}</span></div>}
          {moveStatus && <div className="vela-notice" role="status"><strong>Prompt order saved</strong><span>{moveStatus}</span></div>}
          {prompts.map((prompt, index) => (
            <article key={idOf(prompt) || index}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <textarea aria-label={`Prompt ${index + 1}`} readOnly value={str(prompt.text)} />
              <div>
                <button
                  className="vela-icon-button"
                  type="button"
                  title={moving ? "A new prompt-set version is being saved" : index === 0 ? "Already first" : "Move prompt up into a new version"}
                  aria-label="Move prompt up"
                  disabled={moving || index === 0}
                  onClick={() => void reorderPrompt(index, -1)}
                >{movingIndex === index ? <LoaderCircle className="vela-spin" size={15} /> : <ArrowUp size={15} />}</button>
                <button
                  className="vela-icon-button"
                  type="button"
                  title={moving ? "A new prompt-set version is being saved" : index === prompts.length - 1 ? "Already last" : "Move prompt down into a new version"}
                  aria-label="Move prompt down"
                  disabled={moving || index === prompts.length - 1}
                  onClick={() => void reorderPrompt(index, 1)}
                >{movingIndex === index ? <LoaderCircle className="vela-spin" size={15} /> : <ArrowDown size={15} />}</button>
              </div>
            </article>
          ))}
        </div>
      )}

      {!selectedId && (
        <>
          <Form
            submit="Generate prompt set"
            onSubmit={async form => {
              setGenerationStatus("");
              const result = await api<Row>("/api/prompt-sets/generate", jsonBody({
                instruction,
                count: Number(form.get("count")),
                context: { project_id: initialProject || null },
              }));
              const generated = rows(result.prompts);
              setPromptText(JSON.stringify(generated, null, 2));
              setGenerationStatus(`${str(result.provider)} / ${str(result.model)} returned ${generated.length} editable prompts.`);
            }}
          >
            <div className="form-grid">
              <Field label="Generation instruction">
                <textarea
                  value={instruction}
                  onChange={event => setInstruction(event.target.value)}
                  required
                  placeholder="Create identity, angle, wardrobe, and holdout prompts for this model evaluation."
                />
              </Field>
              <Field label="Prompt count"><input name="count" type="number" min="1" max="50" defaultValue="6" required /></Field>
            </div>
          </Form>
          {generationStatus && <div className="vela-notice" role="status"><strong>Prompts generated</strong><span>{generationStatus}</span></div>}
          <Form
            submit="Save prompt set"
            onSubmit={async form => {
              const values = parsePrompts(promptText);
              if (!values.length) throw new Error("Add at least one prompt before saving.");
              const created = await api<Row>("/api/prompt-sets", jsonBody({
                project_id: form.get("project_id"),
                name: form.get("name"),
                prompts: values,
              }));
              await resource.reload();
              onSelect(idOf(created));
            }}
          >
            <div className="form-grid">
              <Field label="Project"><SelectRecords name="project_id" records={projects} labelKey="title" required value={initialProject || undefined} /></Field>
              <Field label="Name"><input name="name" required /></Field>
            </div>
            <Field label="Editable prompts" hint="Accepts generated prompt objects, a JSON array, or one prompt per line.">
              <textarea name="prompts" value={promptText} onChange={event => setPromptText(event.target.value)} required />
            </Field>
          </Form>
        </>
      )}
    </Panel>
  );
}

export function EvalsScreen({ projectId = "", initialModelVersionId = "" }: { projectId?: string; initialModelVersionId?: string }) {
  const initialParams = new URLSearchParams(location.hash.split("?")[1] ?? "");
  const [scope, setScope] = useState<"project" | "model">(initialParams.get("scope") === "model" ? "model" : "project");
  const [project, setProject] = useState(projectId);
  const [modelId, setModelId] = useState(initialParams.get("model") ?? "");
  const evals = useResource<unknown>(`/api/eval-runs${query({ project_id: project })}`);
  const models = useResource<unknown>(project ? `/api/model-versions${query({ project_id: project })}` : null);
  const modelFamilies = useResource<unknown>(project ? `/api/models${query({ project_id: project })}` : null);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const endpoints = useResource<unknown>("/api/eval-endpoints");
  const queue = useResource<unknown>(`/api/generation-queue${query({ project_id: project })}`);
  const [selectedModels, setSelectedModels] = useState<string[]>(initialModelVersionId ? [initialModelVersionId] : []);
  const [prompts, setPrompts] = useState(() => [newInlinePrompt(""), newInlinePrompt("")]);
  const [message, setMessage] = useState("");
  const generationModels = connectedFalGenerationModels(rows(models.data), project, scope === "model" ? modelId : undefined);
  const evalRunRows = rows(evals.data);
  const latestEval = evalRunRows[0];
  const recentEvalRuns = evalRunRows.slice(0, 3);
  const historicalEvalRuns = evalRunRows.slice(3);
  const selectedGenerationModels = selectedModels.flatMap(id => {
    const model = generationModels.find(item => item.id === id);
    return model ? [model] : [];
  });
  const endpointRows = rows(endpoints.data);
  const selectedEndpointIds = unique(selectedGenerationModels.map(model => model.endpoint ?? ""));
  const selectedEndpointSchemas = selectedEndpointIds.map(endpointId => endpointRows.find(row => str(row.endpoint_id ?? row.id) === endpointId)).filter((row): row is Row => Boolean(row));
  const sharedEndpointSchema = commonEndpointSchema(selectedEndpointSchemas);
  const eligibleModelIds = new Set(generationModels.map(model => model.modelId));
  const readyPromptCount = prompts.reduce((count, prompt) => count + Number(Boolean(prompt.text.trim())), 0);
  const evalPreflightError = !project ? "Select a project." : scope === "model" && !modelId ? "Select a model context." : !selectedModels.length ? "Select at least one model checkpoint." : selectedGenerationModels.length !== selectedModels.length ? "Wait for selected checkpoint details to load." : endpoints.loading || selectedEndpointSchemas.length !== selectedEndpointIds.length ? "Loading settings for the selected FAL endpoints." : !readyPromptCount ? "Add at least one prompt." : "";
  useEffect(() => setSelectedModels(ids => ids.filter(id => generationModels.some(model => model.id === id))), [models.data, project, scope, modelId]);

  return (
    <Page
      title="Eval generator"
      subtitle="Compare the same ordered prompts across one or more provider-registered checkpoints."
      actions={
        <>
          <a className="vela-button" href={`#/grids?project=${projectId}`}><Grid3X3 size={15} /> Grid generator</a>
          {latestEval && <a className="vela-button" href={`#/eval/${idOf(latestEval)}`}><ExternalLink size={15} /> Latest eval</a>}
        </>
      }
    >
      <Notice error={models.error || modelFamilies.error || projects.error || endpoints.error} loading={models.loading || modelFamilies.loading || projects.loading || endpoints.loading} />
      <Panel title="Evaluation context"><div className="form-grid">
        <Field label="Scope"><select value={scope} onChange={event => { setScope(event.target.value as "project" | "model"); setSelectedModels([]); }}><option value="project">Project</option><option value="model">Model / checkpoint</option></select></Field>
        <Field label="Project"><select value={project} onChange={event => { setProject(event.target.value); setModelId(""); setSelectedModels([]); }}><option value="">Select…</option>{rows(projects.data).map(row => <option key={idOf(row)} value={idOf(row)}>{str(row.title ?? row.name)}</option>)}</select></Field>
        {scope === "model" && <Field label="Model"><select value={modelId} onChange={event => { setModelId(event.target.value); setSelectedModels([]); }}><option value="">Select…</option>{rows(modelFamilies.data).filter(row => eligibleModelIds.has(idOf(row))).map(row => <option key={idOf(row)} value={idOf(row)}>{str(row.name)}</option>)}</select></Field>}
      </div><ModelCheckpointPicker models={generationModels} selected={selectedModels} onChange={setSelectedModels} />
      {selectedModels.map(id => { const model = generationModels.find(item => item.id === id); return model && !model.available ? <div className="vela-notice vela-notice-error" role="alert" key={id}>{model.name} / {model.checkpointLabel}: {model.unavailableReason}</div> : null; })}</Panel>
      <div className={`vela-notice${evalPreflightError ? " vela-notice-error" : ""}`} role="status"><strong>Eval preflight</strong><span>{evalPreflightError || `Ready to compare ${readyPromptCount} prompt${readyPromptCount === 1 ? "" : "s"} across ${selectedGenerationModels.length} checkpoint${selectedGenerationModels.length === 1 ? "" : "s"}.`}</span></div>
      <Panel title="Ordered manual prompts"><div className="prompt-editor">
        {prompts.map((prompt, index) => <div className="prompt-editor-row" key={prompt.id}><span>{index + 1}</span><textarea aria-label={`Prompt ${index + 1}`} value={prompt.text} onChange={event => setPrompts(items => items.map(item => item.id === prompt.id ? { ...item, text: event.target.value } : item))} /><button type="button" aria-label={`Move prompt ${index + 1} up`} disabled={!index} onClick={() => setPrompts(moveItem(prompts, index, index - 1))}><ArrowUp size={14} /></button><button type="button" aria-label={`Move prompt ${index + 1} down`} disabled={index === prompts.length - 1} onClick={() => setPrompts(moveItem(prompts, index, index + 1))}><ArrowDown size={14} /></button><button type="button" aria-label={`Remove prompt ${index + 1}`} disabled={prompts.length === 1} onClick={() => setPrompts(items => items.filter(item => item.id !== prompt.id))}><X size={14} /></button></div>)}
        <button type="button" className="vela-button" onClick={() => setPrompts(items => [...items, newInlinePrompt("")])}>Add prompt</button>
      </div></Panel>
      <Panel title="Generation settings">
        <Form
          submit="Generate Eval"
          submitDisabled={Boolean(evalPreflightError)}
          onSubmit={async form => {
            if (!project) throw new Error("Select a project context.");
            const chosen = selectedModels.map(id => generationModels.find(model => model.id === id)).filter(Boolean);
            if (!chosen.length) throw new Error("Select at least one available model checkpoint.");
            if (chosen.some(model => !model?.available)) throw new Error("Remove checkpoints without a usable provider registration.");
            const orderedPrompts = prompts.map(item => ({ prompt_id: item.id, prompt: item.text.trim() })).filter(item => item.prompt);
            if (!orderedPrompts.length) throw new Error("Add at least one prompt.");
            const loraScale = Number(form.get("lora_scale") ?? 1);
            const compiled = [] as Array<{ model: NonNullable<typeof chosen[number]>; requestId: string; admission: Row; parameters: Row }>;
            for (const model of chosen) {
              if (!model?.checkpointRevisionId) throw new Error(`${model?.name} has no immutable checkpoint revision.`);
              const schema = endpointRows.find(row => str(row.endpoint_id ?? row.id) === model.endpoint);
              if (!schema) throw new Error(`FAL settings for ${model.endpoint} are unavailable.`);
              const parameters = { ...parametersFromForm(form, schema), lora_scale: loraScale };
              const requestId = crypto.randomUUID();
              const result = await api<Row>("/api/eval-generation-requests/compile", jsonBody({ workflow: "eval", provider: model.provider, context: scope === "model" ? { kind: "model", project_id: project, model_id: modelId } : { kind: "project", project_id: project }, model_version_id: model.id, checkpoint_revision_id: model.checkpointRevisionId, prompts: orderedPrompts, parameters, client_request_id: requestId }));
              compiled.push({ model, requestId, admission: result.admission as Row, parameters });
            }
            const parentId = crypto.randomUUID();
            const queued = await api<Row>("/api/generation-queue", jsonBody({ workflow: "eval", provider: "fal", context: { kind: scope, project_id: project, model_id: scope === "model" ? modelId : undefined }, model_version_ids: compiled.map(item => item.model.id), compiled_request_id: parentId, client_request_id: parentId, request: { workflow: "eval", context: { kind: scope, projectId: project, modelId }, models: compiled.map(item => item.model), prompts: orderedPrompts, parameters: Object.fromEntries(compiled.map(item => [item.model.id, item.parameters])) }, children: compiled.map(item => ({ model_version_id: item.model.id, compiled_request_id: item.requestId, admission_id: item.admission.id })) }));
            setMessage(`One evaluation job queued as ${idOf(queued)} for ${compiled.length} checkpoint${compiled.length === 1 ? "" : "s"}.`);
            await queue.reload();
          }}
        >
          <div className="form-grid">
            <EndpointFields schema={sharedEndpointSchema} omit={["num_images"]} />
            <Field label="LoRA scale"><input name="lora_scale" type="number" min="0" max="4" step="0.1" defaultValue="1" required /></Field>
          </div>
        </Form>
        {message && <div className="vela-notice" role="status"><strong>Eval queued</strong><span>{message}</span></div>}
      </Panel>
      <GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} />
      <Panel title="Eval Runs">
        <Notice error={evals.error} loading={evals.loading} empty={!evals.loading && !evalRunRows.length} />
        {!!recentEvalRuns.length && <EvalRunStrip runs={recentEvalRuns} />}
        {!!historicalEvalRuns.length && <details className="vela-history-disclosure"><summary>Show {historicalEvalRuns.length} earlier eval run{historicalEvalRuns.length === 1 ? "" : "s"}</summary><EvalRunStrip runs={historicalEvalRuns} /></details>}
      </Panel>
    </Page>
  );
}
function EvalRunStrip({ runs }: { runs: Row[] }) {
  return <div className="eval-run-strip">{runs.map(run => <a href={`#/eval/${idOf(run)}`} key={idOf(run)}><div className="comparison-image">{rows(run.outputs).slice(0, 4).map(output => <AssetImage key={idOf(output)} assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} alt="Eval output preview" loading="lazy" />)}</div><strong>{str(run.name, "Eval run")}</strong><span>{str(run.model_version_name, "Checkpoint")} · {str(run.prompt_count, "0")} prompts · {str(run.output_count, "0")} outputs</span><Status value={run.status} /></a>)}</div>;
}


type InlinePrompt = { id: string; text: string };
function newInlinePrompt(text: string): InlinePrompt { return { id: crypto.randomUUID(), text }; }
export function moveItem<T>(items: T[], from: number, to: number): T[] { const next = [...items]; const [item] = next.splice(from, 1); next.splice(to, 0, item); return next; }
function optionalNumber(value: FormDataEntryValue | null) { if (value == null || value === "") return undefined; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : undefined; }



export function EvalScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/eval-runs/${id}`);
  const outputsResource = useResource<unknown>(`/api/eval-runs/${id}/outputs`);
  const outputs = rows(outputsResource.data).length ? rows(outputsResource.data) : rows(resource.data?.outputs);
  const grouped = useMemo(() => groupBy(outputs, output => str(output.prompt_id, "outputs")), [outputs]);
  const promptIds = useMemo(() => Object.keys(grouped), [grouped]);
  const [selected, setSelected] = useState<string[]>([]);
  const initializedSelection = useRef(false);
  useEffect(() => {
    if (!initializedSelection.current && promptIds.length) {
      initializedSelection.current = true;
      setSelected(promptIds.slice(0, 6));
    }
  }, [promptIds]);
  const shown = Object.entries(grouped).filter(([key]) => selected.includes(key)).slice(0, 6);
  const status = str(resource.data?.status, "unknown");
  const terminal = ["succeeded", "failed", "canceled"].includes(status.toLowerCase());
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
          <strong>{str(resource.data?.name, "Eval viewer")}</strong>
          <span>{resource.data?.prompt_set_name ? str(resource.data.prompt_set_name) : `${str(resource.data?.prompt_count, "0")} inline prompts`} · {str(resource.data?.model_name, "Model")} / {str(resource.data?.model_version_name, "Checkpoint")}</span>
        </div>
        <div className="viewer-actions">
          <a className="vela-button" href="#/models"><ArrowLeft size={15} /> Models</a>
          <a className="vela-button" href={`#/transfers?mode=export&eval=${id}`}>Export</a>
          <AsyncAction
            label="Cancel"
            successLabel="Canceled"
            icon={<X size={15} />}
            disabled={Boolean(cancelDisabledReason)}
            disabledReason={cancelDisabledReason}
            onRun={async () => { await api(`/api/eval-runs/${id}/cancel`, jsonBody({})); await resource.reload(); }}
          />
          <a className="vela-button" href="#/dashboard">Close</a>
        </div>
      </header>
      <main className="vela-main">
        <Notice error={resource.error || outputsResource.error} loading={resource.loading || outputsResource.loading} empty={!resource.loading && !outputsResource.loading && !outputs.length} />
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
                  title={!isSelected && selected.length >= 6 ? "A maximum of six prompts can be compared" : label}
                  onClick={() => setSelected(toggleLimited(selected, key, 6))}
                >{truncate(label, 48)}</button>
              );
            })}
          </div>
          <span>{selected.length}/6 selected</span>
        </div>
        <div className="comparison-columns embedded">
          {shown.map(([promptId, promptOutputs]) => (
            <section className="comparison-column" key={promptId}>
              <header><strong>{promptLabel(promptOutputs[0], promptId)}</strong><span>{promptOutputs.length} images</span></header>
              <p className="prompt-copy">{promptCopy(promptOutputs[0])}</p>
              <GroupComment subjectType="eval_run" subjectId={id} prefix={`Prompt ${promptId}`} />
              {promptOutputs.map(output => (
                <article className="comparison-image" key={idOf(output)}>
                  <a href={galleryOpenHref({ ...output, project_id: resource.data?.project_id, origin_type: "EVAL" })}><AssetImage assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} loading="lazy" alt={promptCopy(output) || "Eval output"} /></a>
                  <RatingControl output={output} />
                  <small>Seed {str(output.seed)}</small>
                </article>
              ))}
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}

export function EvalComparisonScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/generation-queue/${id}/eval-results`);
  const runs = rows(resource.data?.runs);
  const request = resource.data?.request && typeof resource.data.request === "object" ? resource.data.request as Row : {};
  const orderedPrompts = rows(request.prompts);
  const [queryText, setQueryText] = useState("");
  const [visibleModels, setVisibleModels] = useState<string[]>([]);
  useEffect(() => { if (!visibleModels.length && runs.length) setVisibleModels(runs.map(run => str(run.model_version_id))); }, [resource.data]);
  const shownRuns = runs.filter(run => visibleModels.includes(str(run.model_version_id)));
  const shownPrompts = orderedPrompts.filter(prompt => !queryText || str(prompt.prompt).toLowerCase().includes(queryText.toLowerCase()));
  return <Page title="Eval comparison" subtitle="Matching prompts are aligned across selected model checkpoints." actions={<a className="vela-button" href="#/evals"><ArrowLeft size={15} /> Eval Runs</a>}>
    <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !runs.length} />
    <Panel title="Comparison filters"><div className="form-grid"><Field label="Prompt contains"><input value={queryText} onChange={event => setQueryText(event.target.value)} /></Field><Field label="Model checkpoints"><select multiple value={visibleModels} onChange={event => setVisibleModels(Array.from(event.target.selectedOptions, option => option.value))}>{runs.map(run => <option key={str(run.model_version_id)} value={str(run.model_version_id)}>{str(run.model_name)} · {str(run.model_version_name)}</option>)}</select></Field></div></Panel>
    <Panel title="Results"><div className="eval-comparison-matrix" style={{ "--eval-model-count": Math.max(1, shownRuns.length) } as CSSProperties}>
      <div className="eval-comparison-header"><strong>Prompt</strong>{shownRuns.map(run => <strong key={str(run.model_version_id)}>{str(run.model_name)}<small>{str(run.model_version_name)} · step {str(run.checkpoint_step, "—")}</small></strong>)}</div>
      {shownPrompts.map((prompt, promptIndex) => <div className="eval-comparison-row" key={str(prompt.prompt_id, String(promptIndex))}><p>{str(prompt.prompt)}</p>{shownRuns.map(run => { const output = rows(run.outputs).find(item => str(item.prompt_id) === str(prompt.prompt_id)); return <div key={str(run.model_version_id)}>{output ? <a href={galleryOpenHref({ ...output, project_id: resource.data?.project_id, origin_type: "EVAL", eval_run_id: run.eval_run_id })}><AssetImage assetRevisionId={str(output.asset_revision_id ?? output.asset_id)} alt={str(prompt.prompt)} loading="lazy" /></a> : <div className="vela-empty">{str(run.status) === "completed" ? "No output" : str(run.status)}</div>}</div>; })}</div>)}
    </div></Panel>
  </Page>;
}

function RatingControl({ output }: { output: Row }) {
  const [rating, setRating] = useState(0);
  const [saving, setSaving] = useState(0);
  const [error, setError] = useState("");
  return (
    <div className="rating-control compact" aria-label="Rate this eval output">
      {[1, 2, 3, 4, 5].map(value => (
        <button
          key={value}
          type="button"
          aria-label={`${value} star${value === 1 ? "" : "s"}`}
          aria-pressed={rating === value}
          disabled={Boolean(saving)}
          onClick={async () => {
            setSaving(value);
            setError("");
            try {
              await api("/api/reviews", jsonBody({ subject_type: "eval_output", subject_id: idOf(output), rating: value, decision: "candidate" }));
              setRating(value);
            } catch (reason) {
              setError(reason instanceof Error ? reason.message : String(reason));
            } finally {
              setSaving(0);
            }
          }}
        >{saving === value ? <LoaderCircle className="vela-spin" size={13} /> : <Star size={13} fill={rating >= value ? "currentColor" : "none"} />}</button>
      ))}
      {rating > 0 && <Check size={13} aria-label={`Saved ${rating}-star rating`} />}
      {error && <small role="alert">{error}</small>}
    </div>
  );
}

export function GridsScreen({ projectId = "", initialModelVersionId = "" }: { projectId?: string; initialModelVersionId?: string }) {
  const grids = useResource<unknown>(`/api/grids${query({ project_id: projectId })}`);
  const models = useResource<unknown>(`/api/model-versions${query({ project_id: projectId })}`);
  const prompts = useResource<unknown>(`/api/prompt-sets${query({ project_id: projectId })}`);
  const endpoints = useResource<unknown>("/api/eval-endpoints");
  const endpointRows = rows(endpoints.data);
  const modelRows = rows(models.data);
  const [modelVersionId, setModelVersionId] = useState(initialModelVersionId);
  const generationModels = modelRows.map(generationModelFromRow).filter(model => !projectId || model.projectId === projectId);
  const generationModel = generationModels.find(model => model.id === modelVersionId);
  const endpointId = generationModel?.endpoint ?? "";
  const selectedEndpoint = endpointRows.find(row => idOf(row) === endpointId || row.endpoint_id === endpointId);
  const endpointSchema = useResource<Row>(endpointId ? `/api/eval-endpoints/${endpointId}/schema` : null);
  const activeSchema = endpointSchema.data ?? selectedEndpoint;
  const axes = Array.isArray(activeSchema?.grid_axes) ? activeSchema.grid_axes.map(String) : ["prompt", "checkpoint_step", "lora_scale", "seed", "model"];
  const [xValues, setXValues] = useState("front, profile");
  const [yValues, setYValues] = useState("800, 1200, 1600");
  const [xField, setXField] = useState("prompt");
  const [yField, setYField] = useState("checkpoint_step");
  const constrainedValues = (field: string) => axisOptions(field, modelRows, modelVersionId);
  const changeAxis = (field: string, setField: (value: string) => void, setValues: (value: string) => void) => { setField(field); const options = constrainedValues(field); if (options.length) setValues(options.map(option => option.value).join(", ")); };
  useEffect(() => {
    const synchronize = (field: string, value: string, setValue: (next: string) => void) => {
      if (!["checkpoint_step", "model"].includes(field)) return;
      const options = axisOptions(field, modelRows, modelVersionId);
      const allowed = new Set(options.map(option => option.value));
      const selected = csv(value).filter(item => allowed.has(item));
      const next = (selected.length ? selected : options.slice(0, 3).map(option => option.value)).join(", ");
      if (next !== value) setValue(next);
    };
    synchronize(xField, xValues, setXValues);
    synchronize(yField, yValues, setYValues);
  }, [modelVersionId, models.data, xField, yField]);

  return (
    <Page title="Grid generator" subtitle="Compose endpoint-aware two-axis checkpoint sweeps from individual generated images." actions={<a className="vela-button" href={`#/evals?project=${projectId}`}>Eval generator</a>}>
      <Notice error={grids.error || models.error || prompts.error || endpoints.error} loading={models.loading || prompts.loading || endpoints.loading} />
      <Panel title="Grid definition">
        <Notice error={endpointSchema.error} loading={endpointSchema.loading} />
        <Form
          submit="Save grid"
          onSubmit={async form => {
            if (!endpointId) throw new Error("Select an endpoint.");
            if (xField === yField) throw new Error("Choose two different grid axes.");
            const xAxisValues = axisValues(xField, xValues);
            const yAxisValues = axisValues(yField, yValues);
            if (!xAxisValues.length || !yAxisValues.length) throw new Error("Both grid axes need at least one value.");
            const parameters = parametersFromForm(form, activeSchema);
            parameters.lora_scale = Number(form.get("lora_scale") ?? 1);
            await api("/api/grids", jsonBody({
              model_version_id: form.get("model_version_id"),
              prompt_set_id: form.get("prompt_set_id"),
              endpoint_adapter: endpointId,
              x_axis: { field: xField, values: xAxisValues },
              y_axis: { field: yField, values: yAxisValues },
              parameters,
            }));
            await grids.reload();
          }}
        >
          <div className="form-grid">
            <ModelCheckpointPicker multiple={false} models={generationModels} selected={modelVersionId ? [modelVersionId] : []} onChange={ids => setModelVersionId(ids[0] ?? "")} />
            <Field label="Prompt source"><SelectRecords name="prompt_set_id" records={rows(prompts.data)} required /></Field>
            <Field label="Provider / endpoint"><input readOnly value={endpointId ? `FAL / ${endpointId}` : "Select a provider-registered model"} /></Field>
            <Field label="LoRA scale"><input name="lora_scale" type="number" min="0" max="4" step="0.1" defaultValue="1" required /></Field>
            <Field label="Preset">
              <select onChange={event => { const [x, y] = event.target.value.split("|"); changeAxis(x, setXField, setXValues); changeAxis(y, setYField, setYValues); }}>
                <option value="prompt|checkpoint_step">Prompt × Checkpoint</option>
                <option value="checkpoint_step|lora_scale">Checkpoint × LoRA scale</option>
                <option value="checkpoint_step|seed">Checkpoint × Seed</option>
                <option value="prompt|model">Prompt × Model</option>
              </select>
            </Field>
            <Field label="X axis"><select value={xField} onChange={event => changeAxis(event.target.value, setXField, setXValues)}>{axes.map(axis => <option key={axis}>{axis}</option>)}</select></Field>
            <AxisValuesField label="X values" field={xField} value={xValues} onChange={setXValues} options={constrainedValues(xField)} />
            <Field label="Y axis"><select value={yField} onChange={event => changeAxis(event.target.value, setYField, setYValues)}>{axes.map(axis => <option key={axis}>{axis}</option>)}</select></Field>
            <AxisValuesField label="Y values" field={yField} value={yValues} onChange={setYValues} options={constrainedValues(yField)} />
            <EndpointFields key={endpointId} schema={activeSchema} />
          </div>
        </Form>
      </Panel>
      <GridPreview xLabel={xField} yLabel={yField} xValues={csv(xValues)} yValues={csv(yValues)} />
      <Panel title="Saved grids">
        <Notice error={grids.error} loading={grids.loading} empty={!grids.loading && !rows(grids.data).length} />
        <DataTable rows={rows(grids.data)} columns={[["name", "Name"], ["endpoint_adapter", "Endpoint"], ["x_axis", "X axis"], ["y_axis", "Y axis"], ["status", "Status"]]} onRow={row => { location.hash = `grid/${idOf(row)}`; }} />
      </Panel>
    </Page>
  );
}

function AxisValuesField({ label, field, value, onChange, options }: { label: string; field: string; value: string; onChange: (value: string) => void; options: Array<{ value: string; label: string }> }) {
  if (["checkpoint_step", "model"].includes(field)) return <Field label={label}><select multiple size={Math.min(5, Math.max(2, options.length))} value={csv(value)} disabled={!options.length} onChange={event => onChange(Array.from(event.target.selectedOptions, option => option.value).join(", "))}>{options.map(option => <option value={option.value} key={option.value}>{option.label}</option>)}</select></Field>;
  return <Field label={label}><input value={value} onChange={event => onChange(event.target.value)} required /></Field>;
}

function GridPreview({ xLabel, yLabel, xValues, yValues }: { xLabel: string; yLabel: string; xValues: string[]; yValues: string[] }) {
  const [zoom, setZoom] = useState(82);
  return (
    <Panel title="Composed grid preview">
      <div className="grid-preview-tools"><span>X · {xLabel}</span><span>Y · {yLabel}</span><label>Zoom <input aria-label="Preview cell size" type="range" min="52" max="132" value={zoom} onChange={event => setZoom(Number(event.target.value))} /></label></div>
      <div className="grid-board" style={{ "--cell": `${zoom}px`, gridTemplateColumns: `80px repeat(${Math.max(1, xValues.length)}, var(--cell))` } as CSSProperties}>
        <span />
        {xValues.map(value => <strong key={value}>{value}</strong>)}
        {yValues.flatMap(y => [
          <strong key={`${y}-label`}>{y}</strong>,
          ...xValues.map(x => (
            <div
              aria-hidden="true"
              key={`${x}-${y}`}
              style={{
                width: "var(--cell)",
                height: "var(--cell)",
                display: "grid",
                placeItems: "center",
                overflow: "hidden",
                padding: 4,
                border: "1px solid var(--vela-border)",
                background: "var(--vela-surface-soft)",
                color: "var(--vela-muted)",
                fontSize: 10,
                textAlign: "center",
              }}
            ><span>{x}<br />{y}</span></div>
          )),
        ])}
      </div>
    </Panel>
  );
}

export function GridScreen({ id }: { id: string }) {
  const resource = useResource<Row>(`/api/grids/${id}`);
  const cells = rows(resource.data?.cells);
  const jobs = rows(resource.data?.jobs);
  const latestJob = jobs[0];
  const [zoom, setZoom] = useState(82);
  const [selectedCell, setSelectedCell] = useState<Row | null>(null);
  const [billingAck, setBillingAck] = useState(false);
  async function admitGrid() {
    if (!billingAck) throw new Error("A billing acknowledgement is required.");
    const projectId = str(resource.data?.project_id, "");
    const modelVersionId = str(resource.data?.model_version_id, "");
    const checkpointRevisionId = str(resource.data?.checkpoint_revision_id, "");
    if (!projectId || !modelVersionId || !checkpointRevisionId) throw new Error("Grid has no immutable model checkpoint revision.");
    const projection = await api<Row>("/api/fal-admissions", jsonBody({
      kind: "grid",
      project_id: projectId,
      model_version_id: modelVersionId,
      checkpoint_revision_id: checkpointRevisionId,
      grid_definition_id: id,
      parameters: resource.data?.parameters ?? {},
      client_request_id: crypto.randomUUID(),
    }));
    if (projection.kind !== "projection" || !projection.admission) throw new Error(str(projection.detail, "FAL admission is not ready."));
    const admission = projection.admission as Row;
    if (str(admission.state) !== "ready") throw new Error(`FAL admission is ${str(admission.state)}; review storage and checkpoint readiness.`);
    await api(`/api/fal-admissions/${str(admission.id)}/admit`, jsonBody({ expected_version: Number(admission.version), billing_acknowledgement: "I understand FAL may bill this run even if checkpoint fetch fails" }));
    await resource.reload();
  }
  const xValues = unique(cells.map(cell => str(cell.x_value, cell.x_index as string)));
  const yValues = unique(cells.map(cell => str(cell.y_value, cell.y_index as string)));
  const runDisabledReason = resource.loading
    ? "Grid definition is still loading"
    : resource.error
      ? "Grid definition failed to load"
      : !resource.data?.id
        ? "Grid definition is unavailable"
        : undefined;

  return (
    <div className="comparison-viewer vela-route-stage">
      <header className="viewer-topbar">
        <div><strong>{str(resource.data?.name, "Grid viewer")}</strong><span>{str(resource.data?.endpoint_adapter, "Endpoint")} · {cells.length} composed images</span></div>
        <div className="viewer-actions">
          <a className="vela-button" href="#/grids"><ArrowLeft size={15} /> Grid generator</a>
          <a className="vela-button" href="#/grids">Close</a>
          <label className="fal-ack"><input type="checkbox" checked={billingAck} onChange={event => setBillingAck(event.target.checked)} /> I understand FAL may bill this run if checkpoint fetch fails</label><AsyncAction label="Run grid" workingLabel="Admitting..." successLabel="Admitted" icon={<Play size={15} />} disabled={Boolean(runDisabledReason)} disabledReason={runDisabledReason} onRun={admitGrid} />
        </div>
      </header>
      <main className="vela-main">
        <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !cells.length} />
        <Panel title="Grid review">
          <div className="grid-preview-tools">
            <Status value={latestJob?.state ?? (cells.length ? "ready" : "saved")} />
            <span>Selected {selectedCell ? `${str(selectedCell.x_value)} × ${str(selectedCell.y_value)}` : "none"}</span>
            <label>Zoom <input aria-label="Grid cell size" type="range" min="52" max="132" value={zoom} onChange={event => setZoom(Number(event.target.value))} /></label>
          </div>
          {cells.length > 0 && (
            <div className="grid-board image-grid" style={{ "--cell": `${zoom}px`, gridTemplateColumns: `80px repeat(${Math.max(1, xValues.length)}, var(--cell))` } as CSSProperties}>
              <span />
              {xValues.map(value => <strong key={value}>{value}</strong>)}
              {yValues.flatMap(y => [
                <strong key={`${y}-label`}>{y}</strong>,
                ...xValues.map(x => {
                  const cell = cells.find(item => str(item.x_value, item.x_index as string) === x && str(item.y_value, item.y_index as string) === y);
                  const cellAssetId = typeof cell?.asset_revision_id === "string" ? cell.asset_revision_id : typeof cell?.asset_id === "string" ? cell.asset_id : "";
                  return (
                    <div className="grid-cell" key={`${x}-${y}`}>
                      <button
                        className={idOf(selectedCell ?? {}) === idOf(cell ?? {}) ? "active" : ""}
                        type="button"
                        aria-label={`${x}, ${y}${cellAssetId ? "" : ", pending"}`}
                        aria-pressed={idOf(selectedCell ?? {}) === idOf(cell ?? {})}
                        disabled={!cell}
                        onClick={() => cell && setSelectedCell(cell)}
                      >{cellAssetId ? <AssetImage assetRevisionId={cellAssetId} loading="lazy" alt={`${x}, ${y}`} /> : <span>Pending</span>}</button>
                      {cellAssetId && <a className="grid-cell-detail" href={galleryOpenHref({ ...(cell ?? {}), asset_id: cellAssetId, project_id: resource.data?.project_id, origin_type: "EVAL" })}>Details</a>}
                    </div>
                  );
                }),
              ])}
            </div>
          )}
          {Boolean(selectedCell?.asset_revision_id ?? selectedCell?.asset_id) && <a className="vela-button" href={galleryOpenHref({ ...selectedCell, project_id: resource.data?.project_id, origin_type: "EVAL" })}><ExternalLink size={15} /> Open selected image</a>}
        </Panel>
      </main>
    </div>
  );
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
              <Field label="Rating"><select name="rating" defaultValue="3">{[1, 2, 3, 4, 5].map(value => <option key={value}>{value}</option>)}</select></Field>
              <Field label="Decision"><select name="decision"><option>candidate</option><option>approved</option><option>hold</option><option>reject</option></select></Field>
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
    if (definitions.every(definition => definition.custom && typeof definition.custom === "object")) {
      const custom = definitions.map(definition => definition.custom as Row);
      merged.custom = {
        min: Math.max(...custom.map(value => Number(value.min ?? 1))),
        max: Math.min(...custom.map(value => Number(value.max ?? 16_384))),
        step: Math.max(...custom.map(value => Number(value.step ?? 1))),
      };
    }
    return [[field, merged]];
  }));
  return {
    ...first,
    request_fields: common,
    defaults: Object.fromEntries(common.filter(field => defaults[field] !== undefined).map(field => [field, defaults[field]])),
    fields: sharedFields,
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

function csv(value: string) { return value.split(",").map(item => item.trim()).filter(Boolean); }

function axisValues(field: string, value: string): Array<string | number | boolean> {
  return csv(value).map(item => {
    if (["checkpoint_step", "lora_scale", "seed"].includes(field)) {
      const numeric = Number(item);
      if (!Number.isFinite(numeric)) throw new Error(`${field.replaceAll("_", " ")} values must be numbers.`);
      return numeric;
    }
    if (["enable_prompt_expansion", "enable_safety_checker"].includes(field)) {
      if (!/^(true|false)$/i.test(item)) throw new Error(`${field.replaceAll("_", " ")} values must be true or false.`);
      return item.toLowerCase() === "true";
    }
    return item;
  });
}

function axisOptions(field: string, models: Row[], selectedId: string) {
  if (field === "model") return models.map(model => ({ value: idOf(model), label: `${str(model.model_name ?? model.name, "Model")} · step ${str(model.checkpoint_step)}` })).filter(option => option.value);
  if (field === "checkpoint_step") {
    const selected = models.find(model => idOf(model) === selectedId);
    const runId = str(selected?.checkpoint_run_id, "");
    const steps = unique(models.filter(model => runId && str(model.checkpoint_run_id, "") === runId).map(model => str(model.checkpoint_step, "")).filter(Boolean));
    return steps.map(step => ({ value: step, label: `Step ${step}` }));
  }
  return [];
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
