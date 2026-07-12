import { useEffect, useMemo, useState } from "react";
import { ImagePlus } from "lucide-react";
import { api, idOf, listOf, str } from "./api";
import { useResource } from "./hooks";
import { Field, Form, Notice, Page, Panel, Status } from "./ui";
import { falAdapter, generationModelFromRow, normalizedParametersFromMetadata, type GenerationContext, type GenerationModel, type NormalizedGenerationParameters } from "./generation";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

export function ModelCheckpointPicker({ models, selected, onChange, multiple = true }: { models: GenerationModel[]; selected: string[]; onChange: (ids: string[]) => void; multiple?: boolean }) {
  return <Field label={multiple ? "Models / checkpoints" : "Model / checkpoint"} hint="Only checkpoints in the selected context are shown. Unavailable provider registrations cannot be selected.">
    <select aria-label="Models and checkpoints" multiple={multiple} size={multiple ? Math.min(8, Math.max(3, models.length)) : undefined} value={multiple ? selected : selected[0] ?? ""} onChange={event => onChange(Array.from(event.target.selectedOptions, option => option.value))}>
      {!multiple && <option value="">Select...</option>}
      {models.map(model => <option key={model.id} value={model.id} disabled={!model.available}>{model.name} · {model.checkpointLabel} · {model.available ? `${model.provider?.toUpperCase()} / ${model.endpoint}` : model.unavailableReason}</option>)}
    </select>
  </Field>;
}

export function ImageGeneratorScreen({ params }: { params: URLSearchParams }) {
  const initialProject = params.get("project") ?? "";
  const initialModel = params.get("model") ?? "";
  const initialVersion = params.get("model_version") ?? "";
  const initialScope = params.get("scope") === "model" ? "model" : "project";
  const prefillAssetId = params.get("prefill_asset") ?? "";
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [scope, setScope] = useState<"project" | "model">(initialScope);
  const [projectId, setProjectId] = useState(initialProject);
  const [modelId, setModelId] = useState(initialModel);
  const [selected, setSelected] = useState<string[]>(initialVersion ? [initialVersion] : []);
  const modelFamilies = useResource<unknown>(projectId ? `/api/models?project_id=${encodeURIComponent(projectId)}` : null);
  const versions = useResource<unknown>(projectId ? `/api/model-versions?project_id=${encodeURIComponent(projectId)}` : null);
  const assetContext = useResource<Row>(prefillAssetId ? `/api/assets/${prefillAssetId}/context` : null);
  const [prefill, setPrefill] = useState<Partial<NormalizedGenerationParameters>>({ numImages: 1 });
  const [queued, setQueued] = useState("");
  const queue = useResource<unknown>("/api/generation-queue");
  const generationModels = useMemo(() => rows(versions.data).map(generationModelFromRow).filter(model => model.projectId === projectId && (scope !== "model" || !modelId || model.modelId === modelId)), [versions.data, projectId, scope, modelId]);
  useEffect(() => {
    if (!assetContext.data) return;
    const metadata = assetContext.data.metadata && typeof assetContext.data.metadata === "object" ? assetContext.data.metadata as Row : {};
    const asset = assetContext.data.asset && typeof assetContext.data.asset === "object" ? assetContext.data.asset as Row : {};
    const enriched = { ...asset, ...assetContext.data, ...metadata };
    setPrefill(normalizedParametersFromMetadata(enriched));
    const versionId = str(enriched.model_version_id, "");
    const enrichedProject = str(enriched.project_id, "");
    const enrichedModel = str(enriched.model_id, "");
    if (!initialProject && enrichedProject) setProjectId(enrichedProject);
    if (!initialModel && enrichedModel) { setScope("model"); setModelId(enrichedModel); }
    if (versionId) setSelected([versionId]);
  }, [assetContext.data]);
  useEffect(() => { setSelected(ids => ids.filter(id => generationModels.some(model => model.id === id))); }, [generationModels]);
  const context: GenerationContext | null = projectId ? scope === "model" && modelId ? { kind: "model", projectId, modelId, modelVersionId: selected[0] } : { kind: "project", projectId } : null;

  return <Page title="Image Generator" subtitle="Generate through the shared provider layer using a Project or Model/checkpoint context.">
    <Notice error={projects.error || modelFamilies.error || versions.error || assetContext.error} loading={projects.loading || modelFamilies.loading || versions.loading || assetContext.loading} />
    {prefillAssetId && <div className="vela-notice" role="status"><strong>Settings loaded</strong><span>Supported settings from image {prefillAssetId} are prefilled. Nothing will run until you submit.</span></div>}
    <Panel title="Generation context"><div className="form-grid">
      <Field label="Scope"><select value={scope} onChange={event => { setScope(event.target.value as "project" | "model"); setSelected([]); }}><option value="project">Project</option><option value="model">Model / checkpoint</option></select></Field>
      <Field label="Project"><select value={projectId} onChange={event => { setProjectId(event.target.value); setModelId(""); setSelected([]); }}><option value="">Select...</option>{rows(projects.data).map(project => <option value={idOf(project)} key={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>
      {scope === "model" && <Field label="Model"><select value={modelId} onChange={event => { setModelId(event.target.value); setSelected([]); }} disabled={!projectId}><option value="">Select...</option>{rows(modelFamilies.data).map(model => <option value={idOf(model)} key={idOf(model)}>{str(model.name)}</option>)}</select></Field>}
    </div>
    <ModelCheckpointPicker multiple={false} models={generationModels} selected={selected} onChange={setSelected} />
    {context && selected[0] && <p className="vela-meta">Provider and endpoint: {generationModels.find(model => model.id === selected[0])?.provider?.toUpperCase()} / {generationModels.find(model => model.id === selected[0])?.endpoint}</p>}
    </Panel>
    <Panel title="Image request"><Form submit="Generate image" onSubmit={async form => {
      if (!context) throw new Error("Select a project context.");
      const model = generationModels.find(item => item.id === selected[0]);
      if (!model) throw new Error("Select an available model checkpoint.");
      const parameters: NormalizedGenerationParameters = { prompt: String(form.get("prompt") ?? ""), negativePrompt: String(form.get("negative_prompt") ?? "") || undefined, seed: optionalNumber(form.get("seed")), width: optionalNumber(form.get("width")), height: optionalNumber(form.get("height")), guidance: optionalNumber(form.get("guidance")), steps: optionalNumber(form.get("steps")), sampler: String(form.get("sampler") ?? "") || undefined, scheduler: String(form.get("scheduler") ?? "") || undefined, loraScale: optionalNumber(form.get("lora_scale")), numImages: 1 };
      const result = await falAdapter.submit({ workflow: "image", context, model, parameters, clientRequestId: crypto.randomUUID() });
      setQueued(result.providerRequestId ? `Queued as ${result.providerRequestId}.` : "Generation queued."); await queue.reload();
    }}><div className="form-grid">
      <Field label="Prompt"><textarea name="prompt" required key={`prompt-${prefill.prompt}`} defaultValue={prefill.prompt ?? ""} /></Field>
      <Field label="Negative prompt"><textarea name="negative_prompt" key={`negative-${prefill.negativePrompt}`} defaultValue={prefill.negativePrompt ?? ""} /></Field>
      <Field label="Seed"><input name="seed" type="number" key={`seed-${prefill.seed}`} defaultValue={prefill.seed} /></Field>
      <Field label="Width"><input name="width" type="number" min="1" key={`width-${prefill.width}`} defaultValue={prefill.width} /></Field>
      <Field label="Height"><input name="height" type="number" min="1" key={`height-${prefill.height}`} defaultValue={prefill.height} /></Field>
      <Field label="Guidance"><input name="guidance" type="number" step="0.1" key={`guidance-${prefill.guidance}`} defaultValue={prefill.guidance} /></Field>
      <Field label="Steps"><input name="steps" type="number" min="1" key={`steps-${prefill.steps}`} defaultValue={prefill.steps} /></Field>
      <Field label="Sampler"><input name="sampler" key={`sampler-${prefill.sampler}`} defaultValue={prefill.sampler} /></Field>
      <Field label="Scheduler"><input name="scheduler" key={`scheduler-${prefill.scheduler}`} defaultValue={prefill.scheduler} /></Field>
      <Field label="LoRA scale"><input name="lora_scale" type="number" min="0" max="4" step="0.1" key={`scale-${prefill.loraScale}`} defaultValue={prefill.loraScale ?? 1} /></Field>
    </div></Form>{queued && <div className="vela-notice" role="status"><ImagePlus size={16} /><span>{queued}</span></div>}</Panel>
    <GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} />
  </Page>;
}

export function GenerationQueue({ items, error, loading }: { items: Row[]; error?: string; loading?: boolean }) {
  return <Panel title="Generation queue"><Notice error={error} loading={loading} empty={!loading && !items.length} />
    {!!items.length && <div className="table-wrap"><table><thead><tr><th>Order</th><th>Workflow</th><th>Context</th><th>Models</th><th>Status</th><th>Progress / result</th></tr></thead><tbody>{items.map((item, index) => {
      const progress = item.progress && typeof item.progress === "object" ? item.progress as Row : {};
      const request = item.request && typeof item.request === "object" ? item.request as Row : {};
      const context = request.context && typeof request.context === "object" ? request.context as Row : {};
      const model = request.model && typeof request.model === "object" ? request.model as Row : {};
      const modelIds = Array.isArray(item.model_version_ids) ? item.model_version_ids.map(String) : [];
      const projectLabel = str(context.projectTitle ?? context.projectName, str(item.project_id));
      const modelLabel = str(model.name, str(item.model_id, ""));
      const checkpointLabel = str(model.checkpointLabel, "");
      const workflow = str(item.workflow);
      const workflowLink = workflow === "eval" ? <a href={`#/eval-comparison/${idOf(item)}`}>Eval</a> : workflow === "grid" ? <a href={`#/grid/${idOf(item)}`}>Grid</a> : workflow;
      return <tr key={idOf(item)}><td>{index + 1}</td><td>{workflowLink}</td><td>{projectLabel}{modelLabel ? ` / ${modelLabel}` : ""}</td><td>{checkpointLabel || Array.from(new Set(modelIds)).join(", ")}</td><td><Status value={str(item.status)} /></td><td>{item.error ? str(item.error) : `${str(progress.completed, "0")} / ${str(progress.total, "-")}`}</td></tr>;
    })}</tbody></table></div>}
  </Panel>;
}

export function GenerationQueueScreen() {
  const queue = useResource<unknown>("/api/generation-queue");
  return <Page title="Generation queue" subtitle="One durable queue for Image, Eval, and Grid workflows."><GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} /></Page>;
}

function optionalNumber(value: FormDataEntryValue | null) { if (value == null || value === "") return undefined; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : undefined; }
