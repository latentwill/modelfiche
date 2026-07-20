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
import { EndpointFields, GenerationQueue, LoraScaleField, ModelCheckpointPicker, parametersFromForm } from "./generator";
import { connectedFalGenerationModels } from "./generation";
import { galleryOpenHref } from "./screens-assets";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const PROGRESS_POLL = { pollInterval: 1000, pollWhile: pollWhileActive };
const OUTPUTS_POLL = { pollInterval: 1000 };

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
  const [promptCount, setPromptCount] = useState(6);
  const [newSetProject, setNewSetProject] = useState(initialProject);
  const [newSetName, setNewSetName] = useState("");
  const moveLock = useRef(false);
  const prompts = rows(selected.data?.prompts);
  const moving = movingIndex !== null;
  useEffect(() => { if (initialProject) setNewSetProject(initialProject); }, [initialProject]);
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
          <Dropdown
            aria-label="Saved set"
            value={selectedId}
            onChange={onSelect}
            options={[
              { value: "", label: "New prompt set..." },
              ...rows(resource.data).map(set => ({ value: idOf(set), label: `${str(set.name)} · v${str(set.version, "1")}` })),
            ]}
          />
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
          {selected.error && <button type="button" className="vela-button" onClick={() => void selected.reload()}>Retry saved version</button>}
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
            submitDisabled={!instruction.trim()}
            onSubmit={async () => {
              if (!instruction.trim()) throw new Error("Enter a generation instruction.");
              setGenerationStatus("");
              const result = await api<Row>("/api/prompt-sets/generate", jsonBody({
                instruction,
                count: promptCount,
                context: { project_id: newSetProject || null },
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
              <Field label="Prompt count"><input name="count" type="number" min="1" max="50" value={promptCount} onChange={event => setPromptCount(Number(event.target.value))} required /></Field>
            </div>
          </Form>
          {generationStatus && <div className="vela-notice" role="status"><strong>Prompts generated</strong><span>{generationStatus}</span></div>}
          <Form
            submit="Save prompt set"
            submitDisabled={!newSetProject || !newSetName.trim() || !parsePrompts(promptText).length}
            onSubmit={async () => {
              const values = parsePrompts(promptText);
              if (!values.length) throw new Error("Add at least one prompt before saving.");
              const created = await api<Row>("/api/prompt-sets", jsonBody({
                project_id: newSetProject,
                name: newSetName,
                prompts: values,
              }));
              await resource.reload();
              onSelect(idOf(created));
            }}
          >
            <div className="form-grid">
              <Field label="Project"><Dropdown aria-label="Project" value={newSetProject} options={[{ value: "", label: "Select…" }, ...projects.map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} onChange={setNewSetProject} /></Field>
              <Field label="Name"><input name="name" value={newSetName} onChange={event => setNewSetName(event.target.value)} required /></Field>
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
  const [selectedModels, setSelectedModels] = useState<string[]>(initialModelVersionId ? [initialModelVersionId] : []);
  const [selectedPromptSetId, setSelectedPromptSetId] = useState(initialParams.get("prompt_set") ?? "");
  const [prompts, setPrompts] = useState(() => [newInlinePrompt(""), newInlinePrompt("")]);
  const [settingsRevision, setSettingsRevision] = useState(0);
  const [settingsValid, setSettingsValid] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const [message, setMessage] = useState("");
  const evals = useResource<unknown>(`/api/eval-runs${query({ project_id: project })}`, PROGRESS_POLL);
  const models = useResource<unknown>(project ? `/api/model-versions${query({ project_id: project })}` : null);
  const modelFamilies = useResource<unknown>(project ? `/api/models${query({ project_id: project })}` : null);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const queue = useResource<unknown>(`/api/generation-queue${query({ project_id: project })}`, PROGRESS_POLL);
  const endpoints = useResource<unknown>("/api/eval-endpoints");
  const promptSets = useResource<unknown>(project ? `/api/prompt-sets${query({ project_id: project })}` : null);
  const promptSet = useResource<Row>(selectedPromptSetId ? `/api/prompt-sets/${selectedPromptSetId}` : null);
  const savedGrids = useResource<unknown>(project ? `/api/grids${query({ project_id: project })}` : null);
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
  const selectedProject = rows(projects.data).find(item => idOf(item) === project);
  const evalPreflightError =
    !project ? "Select a project." :
      scope === "model" && !modelId ? "Select a model context." :
        !selectedModels.length ? "Select at least one model checkpoint." :
          selectedGenerationModels.length !== selectedModels.length ? "Wait for selected checkpoint details to load." :
            endpoints.loading || selectedEndpointSchemas.length !== selectedEndpointIds.length ? "Loading settings for the selected FAL endpoints." :
              selectedPromptSetId && promptSet.loading ? "Loading the selected prompt set version." :
                promptSet.error ? promptSet.error :
                  selectedPromptSetId && !rows(promptSet.data?.prompts).length ? "The selected prompt set version is empty." :
                    !readyPromptCount ? "Add at least one prompt." :
                      !settingsValid ? "Complete the endpoint-defined settings within their allowed bounds." : "";

  useEffect(() => setSelectedModels(ids => ids.filter(id => generationModels.some(model => model.id === id))), [models.data, project, scope, modelId]);
  useEffect(() => {
    if (!selectedPromptSetId || !promptSet.data) return;
    setPrompts(rows(promptSet.data.prompts).map(prompt => ({ id: idOf(prompt), text: str(prompt.text) })));
  }, [selectedPromptSetId, promptSet.data]);
  useEffect(() => {
    setSettingsValid(Boolean(settingsRef.current?.closest("form")?.checkValidity()));
  }, [sharedEndpointSchema, settingsRevision]);

  return (
    <Page
      title="Eval generator"
      subtitle="Compare the same ordered prompt-set version across one or more provider-registered checkpoints."
      actions={
        <>
          <a className="vela-button" href={`#/prompt-sets?project=${project}`}>Prompt sets</a>
          <a className="vela-button" href={`#/grids?project=${project}${selectedPromptSetId ? `&prompt_set=${selectedPromptSetId}` : ""}`}><Grid3X3 size={15} /> Grid generator</a>
          {latestEval && <a className="vela-button" href={`#/eval/${idOf(latestEval)}`}><ExternalLink size={15} /> Latest eval</a>}
        </>
      }
    >
      <Notice error={models.error || modelFamilies.error || projects.error || endpoints.error || promptSets.error} loading={models.loading || modelFamilies.loading || projects.loading || endpoints.loading || promptSets.loading} />
      <Panel title="Evaluation context"><div className="form-grid">
        <Field label="Scope">
          <Dropdown
            aria-label="Scope"
            value={scope}
            onChange={value => { setScope(value as "project" | "model"); setSelectedModels([]); }}
            options={[
              { value: "project", label: "Project" },
              { value: "model", label: "Model / checkpoint" },
            ]}
          />
        </Field>
        <Field label="Project">
          <Dropdown
            aria-label="Project"
            value={project}
            onChange={value => { setProject(value); setModelId(""); setSelectedModels([]); setSelectedPromptSetId(""); setPrompts([newInlinePrompt(""), newInlinePrompt("")]); }}
            options={[
              { value: "", label: "Select…" },
              ...rows(projects.data).map(row => ({ value: idOf(row), label: str(row.title ?? row.name) })),
            ]}
          />
        </Field>
        {scope === "model" && <Field label="Model">
          <Dropdown
            aria-label="Model"
            value={modelId}
            onChange={value => { setModelId(value); setSelectedModels([]); }}
            options={[
              { value: "", label: "Select…" },
              ...rows(modelFamilies.data)
                .filter(row => eligibleModelIds.has(idOf(row)))
                .map(row => ({ value: idOf(row), label: str(row.name) })),
            ]}
          />
        </Field>}
      </div><ModelCheckpointPicker mode="multiple" models={generationModels} selected={selectedModels} onChange={setSelectedModels} />
      {selectedModels.map(id => { const model = generationModels.find(item => item.id === id); return model && !model.available ? <div className="vela-notice vela-notice-error" role="alert" key={id}>{model.name} / {model.checkpointLabel}: {model.unavailableReason}</div> : null; })}</Panel>
      <Panel title={selectedPromptSetId ? "Prompt set version" : "Ordered manual prompts"}>
        <div className="form-grid"><Field label="Prompt source"><Dropdown aria-label="Prompt set version" value={selectedPromptSetId} disabled={!project} options={[{ value: "", label: "Manual prompts" }, ...rows(promptSets.data).map(item => ({ value: idOf(item), label: `${str(item.name)} · v${str(item.version, "1")} · ${str(item.prompt_count, "0")} prompts` }))]} onChange={value => { setSelectedPromptSetId(value); if (!value) setPrompts([newInlinePrompt(""), newInlinePrompt("")]); }} /></Field></div>
        <Notice error={promptSet.error} loading={promptSet.loading} />
        {selectedPromptSetId && promptSet.data && <div className="vela-notice" role="status"><strong>Immutable source selected</strong><span>{str(promptSet.data.name)} · version {str(promptSet.data.version, "1")} supplies {prompts.length} ordered prompts. Use an editable copy to change text without altering the saved version.</span></div>}
        <div className="prompt-editor">
          {prompts.map((prompt, index) => <div className="prompt-editor-row" key={prompt.id}><span>{index + 1}</span><textarea aria-label={`Prompt ${index + 1}`} readOnly={Boolean(selectedPromptSetId)} value={prompt.text} onChange={event => setPrompts(items => items.map(item => item.id === prompt.id ? { ...item, text: event.target.value } : item))} /><button type="button" aria-label={`Move prompt ${index + 1} up`} disabled={Boolean(selectedPromptSetId) || !index} onClick={() => setPrompts(moveItem(prompts, index, index - 1))}><ArrowUp size={14} /></button><button type="button" aria-label={`Move prompt ${index + 1} down`} disabled={Boolean(selectedPromptSetId) || index === prompts.length - 1} onClick={() => setPrompts(moveItem(prompts, index, index + 1))}><ArrowDown size={14} /></button><button type="button" aria-label={`Remove prompt ${index + 1}`} disabled={Boolean(selectedPromptSetId) || prompts.length === 1} onClick={() => setPrompts(items => items.filter(item => item.id !== prompt.id))}><X size={14} /></button></div>)}
          {selectedPromptSetId ? <button type="button" className="vela-button" onClick={() => setSelectedPromptSetId("")}>Use editable copy</button> : <button type="button" className="vela-button" onClick={() => setPrompts(items => [...items, newInlinePrompt("")])}>Add prompt</button>}
        </div>
      </Panel>
      <div className={`vela-notice${evalPreflightError ? " vela-notice-error" : ""}`} role="status"><strong>Eval preflight</strong><span>{evalPreflightError || `${selectedEndpointIds.join(", ")} accepts ${readyPromptCount * selectedGenerationModels.length} requests: ${readyPromptCount} prompts across ${selectedGenerationModels.length} checkpoints.`}</span></div>
      <Panel title="Generation settings">
        <Form
          submit="Generate Eval"
          submitDisabled={Boolean(evalPreflightError)}
          onSubmit={async form => {
            if (evalPreflightError) throw new Error(evalPreflightError);
            const chosen = selectedModels.map(id => generationModels.find(model => model.id === id)).filter(Boolean);
            const orderedPrompts = prompts.map(item => ({ prompt_id: item.id, prompt: item.text.trim() })).filter(item => item.prompt);
            const loraScale = Number(form.get("lora_scale") ?? 1);
            const compiled = [] as Array<{ model: NonNullable<typeof chosen[number]>; requestId: string; admission: Row; parameters: Row }>;
            for (const model of chosen) {
              if (!model?.checkpointRevisionId) throw new Error(`${model?.name} has no immutable checkpoint revision.`);
              const schema = endpointRows.find(row => str(row.endpoint_id ?? row.id) === model.endpoint);
              if (!schema) throw new Error(`FAL settings for ${model.endpoint} are unavailable.`);
              const parameters = { ...parametersFromForm(form, schema), lora_scale: loraScale };
              const requestId = crypto.randomUUID();
              const result = await api<Row>("/api/eval-generation-requests/compile", jsonBody({ workflow: "eval", provider: model.provider, context: scope === "model" ? { kind: "model", project_id: project, model_id: modelId } : { kind: "project", project_id: project }, model_version_id: model.id, checkpoint_revision_id: model.checkpointRevisionId, prompt_set_id: selectedPromptSetId || undefined, prompts: orderedPrompts, parameters, client_request_id: requestId }));
              compiled.push({ model, requestId, admission: result.admission as Row, parameters });
            }
            const parentId = crypto.randomUUID();
            const queued = await api<Row>("/api/generation-queue", jsonBody({ workflow: "eval", provider: "fal", context: { kind: scope, project_id: project, model_id: scope === "model" ? modelId : undefined }, model_version_ids: compiled.map(item => item.model.id), compiled_request_id: parentId, client_request_id: parentId, request: { workflow: "eval", endpoint_ids: selectedEndpointIds, context: { kind: scope, projectId: project, projectTitle: str(selectedProject?.title ?? selectedProject?.name, project), modelId }, models: compiled.map(item => item.model), prompt_set: selectedPromptSetId ? { id: selectedPromptSetId, name: str(promptSet.data?.name), version: promptSet.data?.version } : null, prompts: orderedPrompts, parameters: Object.fromEntries(compiled.map(item => [item.model.id, item.parameters])) }, children: compiled.map(item => ({ model_version_id: item.model.id, compiled_request_id: item.requestId, admission_id: item.admission.id })) }));
            setMessage(`One evaluation job queued as ${idOf(queued)} for ${compiled.length} checkpoint${compiled.length === 1 ? "" : "s"}.`);
            await queue.reload();
          }}
        >
          <div ref={settingsRef}>
            <div className="form-grid">
              <EndpointFields schema={sharedEndpointSchema} omit={["num_images"]} onChange={() => setSettingsRevision(value => value + 1)} />
              <LoraScaleField schema={sharedEndpointSchema} onChange={() => setSettingsRevision(value => value + 1)} />
            </div>
          </div>
        </Form>
        {message && <div className="vela-notice" role="status"><strong>Eval queued</strong><span>{message}</span></div>}
      </Panel>
      <GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} />
      <Panel title="Prompt Sets and saved Grids"><div className="eval-saved-index"><section><h3>Prompt sets</h3>{rows(promptSets.data).length ? rows(promptSets.data).map(item => <a key={idOf(item)} href={`#/prompt-sets/${idOf(item)}?project=${project}`}>{str(item.name)} · v{str(item.version, "1")}</a>) : <span className="vela-meta">No saved prompt sets.</span>}</section><section><h3>Saved grids</h3>{rows(savedGrids.data).length ? rows(savedGrids.data).map(item => <a key={idOf(item)} href={`#/grid/${idOf(item)}`}>{str(item.name, "Saved grid")} · {str(item.endpoint_adapter, "Endpoint unavailable")}</a>) : <span className="vela-meta">No saved Grids.</span>}</section></div></Panel>
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
  const returnHref = `#/evals${projectId ? `?project=${projectId}&scope=project` : ""}`;
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
          {resource.data && <span>{resource.data.prompt_set_name ? `${str(resource.data.prompt_set_name)} · saved version` : `${str(resource.data.prompt_count, "0")} inline prompts`} · {str(resource.data.model_name, "Model")} / {str(resource.data.model_version_name, "Checkpoint")}</span>}
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
