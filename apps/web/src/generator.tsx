import { useEffect, useMemo, useState } from "react";
import { ImagePlus } from "lucide-react";
import { api, idOf, listOf, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { Dropdown, Field, Form, Notice, Page, Panel, Status } from "./ui";
import { falAdapter, generationModelFromRow, generationModelUnavailableReason, pickerModeMultiple, normalizedParametersFromMetadata, type GenerationContext, type GenerationModel, type GenerationPickerMode, type NormalizedGenerationParameters } from "./generation";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

export type ModelCheckpointPickerProps = {
  models: GenerationModel[];
  selected: string[];
  onChange: (ids: string[]) => void;
  mode: GenerationPickerMode;
  label?: string;
};

/** Shared stable-ID checkpoint picker. Unavailable registrations remain visible and explain why. */
export function ModelCheckpointPicker({ models, selected, onChange, mode, label }: ModelCheckpointPickerProps) {
  const isMultiple = pickerModeMultiple(mode);
  const selectedIds = selected.filter(id => models.some(model => model.id === id));
  const pickerLabel = label ?? (mode === "single" ? "Model and checkpoint" : "Models / checkpoints");
  const ariaLabel = mode === "single" ? pickerLabel : mode === "fixed" ? "Fixed model and checkpoint" : mode === "axis" ? "Axis model checkpoints" : "Models and checkpoints";
  if (!isMultiple) {
    return <Field label={pickerLabel}>
      <Dropdown
        aria-label={ariaLabel}
        value={selectedIds[0] ?? ""}
        options={[
          { value: "", label: "Select..." },
          ...models.map(model => {
            const reason = generationModelUnavailableReason(model);
            return { value: model.id, label: `${model.name} · ${model.checkpointLabel} · ${reason || `${model.provider?.toUpperCase()} / ${model.endpoint}`}`, disabled: Boolean(reason) };
          }),
        ]}
        onChange={id => onChange(id ? [id] : [])}
      />
    </Field>;
  }
  return <Field label={pickerLabel}>
    <select className="vela-multi-select" aria-label={ariaLabel} multiple size={Math.min(8, Math.max(3, models.length))} value={selectedIds} onChange={event => onChange(Array.from(event.target.selectedOptions, option => option.value))}>
      {models.map(model => {
        const reason = generationModelUnavailableReason(model);
        return <option key={model.id} value={model.id} disabled={Boolean(reason)} title={reason || undefined}>{model.name} · {model.checkpointLabel} · {reason || `${model.provider?.toUpperCase()} / ${model.endpoint}`}</option>;
      })}
    </select>
  </Field>;
}

const endpointFieldExclusions: Record<string, true> = { sync_mode: true, prompt: true, loras: true };
const imageSizeLabels: Record<string, string> = {
  square_hd: "Square HD",
  square: "Square",
  portrait_4_3: "Portrait 4:3",
  portrait_16_9: "Portrait 16:9",
  landscape_4_3: "Landscape 4:3",
  landscape_16_9: "Landscape 16:9",
};

export function EndpointFields({ schema, omit = [] }: { schema?: Row; omit?: string[] }) {
  const defaults = (schema?.defaults ?? {}) as Row;
  const fieldSchema = (schema?.fields ?? {}) as Record<string, Row>;
  const requestFields = Array.isArray(schema?.request_fields) ? schema.request_fields.map(String) : [];
  const omitted = new Set(omit);
  const fields = Array.from(new Set([...Object.keys(defaults), ...requestFields])).filter(key => !endpointFieldExclusions[key] && !omitted.has(key));
  return <>{fields.map(key => <EndpointField key={key} field={key} value={defaults[key]} schema={fieldSchema[key]} />)}</>;
}

function EndpointField({ field, value, schema = {} }: { field: string; value: unknown; schema?: Row }) {
  const label = field === "image_size" ? "Aspect ratio / resolution" : field.replaceAll("_", " ");
  const type = str(schema.type, typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "string");
  const options = Array.isArray(schema.options) ? schema.options.map(String) : [];
  const [imageSize, setImageSize] = useState(typeof value === "object" ? "custom" : str(value, options[0]));
  if (type === "boolean") return <Field label={label}><Dropdown aria-label={label} name={field} defaultValue={String(value ?? false)} options={[{ value: "true", label: "Enabled" }, { value: "false", label: "Disabled" }]} /></Field>;
  if (type === "image_size") {
    const custom = (schema.custom ?? {}) as Row;
    const current = typeof value === "object" && value ? value as Row : {};
    return <><Field label={label} hint="Presets come from the selected FAL endpoint. Use custom dimensions only when exact pixels are required."><Dropdown aria-label={label} name={field} value={imageSize} onChange={setImageSize} options={[...options.map(option => ({ value: option, label: imageSizeLabels[option] ?? option.replaceAll("_", " ") })), { value: "custom", label: "Custom dimensions" }]} /></Field>{imageSize === "custom" && <><Field label="Custom width"><input name={`${field}_width`} type="number" min={Number(custom.min ?? 1)} max={Number(custom.max ?? 16384)} step={Number(custom.step ?? 1)} defaultValue={str(current.width, "1024")} required /></Field><Field label="Custom height"><input name={`${field}_height`} type="number" min={Number(custom.min ?? 1)} max={Number(custom.max ?? 16384)} step={Number(custom.step ?? 1)} defaultValue={str(current.height, "1024")} required /></Field></>}
    </>;
  }
  if (type === "enum" && options.length) return <Field label={label}><Dropdown aria-label={label} name={field} defaultValue={str(value, options[0])} options={options.map(option => ({ value: option, label: option }))} /></Field>;
  if (type === "integer" || type === "number") return <Field label={label}><input name={field} type="number" min={schema.min === undefined ? undefined : Number(schema.min)} max={schema.max === undefined ? undefined : Number(schema.max)} step={schema.step === undefined ? (type === "integer" ? 1 : "any") : Number(schema.step)} defaultValue={value === undefined ? "" : Number(value)} /></Field>;
  return <Field label={label}><input name={field} defaultValue={str(value, "")} /></Field>;
}

export function parametersFromForm(form: FormData, schema?: Row) {
  const defaults = (schema?.defaults ?? {}) as Row;
  const fieldSchema = (schema?.fields ?? {}) as Record<string, Row>;
  const requestFields = Array.isArray(schema?.request_fields) ? schema.request_fields.map(String) : [];
  const parameters: Row = {};
  for (const key of Array.from(new Set([...Object.keys(defaults), ...requestFields]))) {
    if (endpointFieldExclusions[key]) continue;
    const fallback = defaults[key];
    const raw = form.get(key);
    if (raw === null || raw === "") {
      if (fallback !== undefined) parameters[key] = fallback;
      continue;
    }
    if (fieldSchema[key]?.type === "image_size" && raw === "custom") {
      parameters[key] = { width: Number(form.get(`${key}_width`)), height: Number(form.get(`${key}_height`)) };
    } else if (fieldSchema[key]?.type === "boolean" || typeof fallback === "boolean") {
      parameters[key] = raw === "true";
    } else if (["integer", "number"].includes(str(fieldSchema[key]?.type, "")) || typeof fallback === "number") {
      parameters[key] = Number(raw);
    } else {
      parameters[key] = raw;
    }
  }
  parameters.sync_mode = false;
  return parameters;
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
  // Do not clear route/replay IDs while the real version registration is loading.
  useEffect(() => {
    if (versions.data == null) return;
    setSelected(ids => ids.filter(id => generationModels.some(model => model.id === id)));
  }, [generationModels, versions.data]);
  const context: GenerationContext | null = projectId ? scope === "model" ? modelId ? { kind: "model", projectId, modelId, modelVersionId: selected[0] } : null : { kind: "project", projectId } : null;
  const selectedModel = generationModels.find(model => model.id === selected[0]);
  const endpointSchema = useResource<Row>(selectedModel?.endpoint ? `/api/eval-endpoints/${selectedModel.endpoint}/schema` : null);
  const imageSchema = useMemo(() => endpointSchema.data ? {
    ...endpointSchema.data,
    defaults: {
      ...((endpointSchema.data.defaults ?? {}) as Row),
      ...(prefill.width && prefill.height ? { image_size: { width: prefill.width, height: prefill.height } } : {}),
    },
  } : undefined, [endpointSchema.data, prefill.width, prefill.height]);
  const imagePreflightError = !projectId ? "Select a project." : scope === "model" && !modelId ? "Select a model context." : !selectedModel ? "Select an available model checkpoint." : !selectedModel.available ? selectedModel.unavailableReason || "Select a provider-registered checkpoint." : endpointSchema.loading || !endpointSchema.data ? "Loading settings for the selected FAL endpoint." : "";
  return <Page title="Image Generator" subtitle="Generate through the shared provider layer using the selected project/model context.">
    <Notice error={projects.error || modelFamilies.error || versions.error || assetContext.error || endpointSchema.error} loading={projects.loading || modelFamilies.loading || versions.loading || assetContext.loading} />
    {prefillAssetId && <div className="generation-source-layout"><figure className="generation-image-frame"><AssetImage assetRevisionId={prefillAssetId} variant="content" alt="Source image for generation settings" /><figcaption>Source image · settings reference</figcaption></figure><div className="vela-notice" role="status"><strong>Settings loaded</strong><span>Supported settings from image {prefillAssetId} are prefilled. Nothing will run until you submit.</span></div></div>}
    <Panel title="Generation context"><div className="form-grid">
      <Field label="Scope"><Dropdown aria-label="Scope" value={scope} options={[{ value: "project", label: "Project" }, { value: "model", label: "Model / checkpoint" }]} onChange={value => { setScope(value as "project" | "model"); setModelId(""); setSelected([]); }} /></Field>
      <Field label="Project"><Dropdown aria-label="Project" value={projectId} options={[{ value: "", label: "Select..." }, ...rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} onChange={value => { setProjectId(value); setModelId(""); setSelected([]); }} /></Field>
      {scope === "model" && <Field label="Model"><Dropdown aria-label="Model" value={modelId} disabled={!projectId} options={[{ value: "", label: "Select..." }, ...rows(modelFamilies.data).map(model => ({ value: idOf(model), label: str(model.name) }))]} onChange={value => { setModelId(value); setSelected([]); }} /></Field>}
    </div>
    <ModelCheckpointPicker mode="single" models={generationModels} selected={selected} onChange={setSelected} label="Model / checkpoint" />
    {context && selected[0] && <p className="vela-meta">Provider and endpoint: {generationModels.find(model => model.id === selected[0])?.provider?.toUpperCase()} / {generationModels.find(model => model.id === selected[0])?.endpoint}</p>}
    </Panel>
    <div className={`vela-notice${imagePreflightError ? " vela-notice-error" : ""}`} role="status"><strong>Generation preflight</strong><span>{imagePreflightError || `Ready for ${selectedModel?.provider?.toUpperCase()} / ${selectedModel?.endpoint}. Enter a prompt, then generate one image.`}</span></div>
    <Panel title="Image request"><Form submit="Generate image" submitDisabled={Boolean(imagePreflightError)} onSubmit={async form => {
      if (!context) throw new Error("Select a project context.");
      const model = generationModels.find(item => item.id === selected[0]);
      if (!model) throw new Error("Select an available model checkpoint.");
      const endpointParameters = parametersFromForm(form, imageSchema);
      const parameters: NormalizedGenerationParameters = { prompt: String(form.get("prompt") ?? ""), seed: optionalNumber(form.get("seed")), loraScale: optionalNumber(form.get("lora_scale")), numImages: 1, extra: endpointParameters };
      const result = await falAdapter.submit({ workflow: "image", context, model, parameters, clientRequestId: crypto.randomUUID() });
      setQueued(result.providerRequestId ? `Queued as ${result.providerRequestId}.` : "Generation queued."); await queue.reload();
    }}><div className="form-grid">
      <Field label="Prompt"><textarea name="prompt" required key={`prompt-${prefill.prompt}`} defaultValue={prefill.prompt ?? ""} /></Field>
      <Field label="Seed"><input name="seed" type="number" key={`seed-${prefill.seed}`} defaultValue={prefill.seed} /></Field>
      <EndpointFields key={`${selectedModel?.endpoint}-${prefill.width}-${prefill.height}`} schema={imageSchema} omit={["num_images", "seed"]} />
      <Field label="LoRA scale"><input name="lora_scale" type="number" min="0" max="4" step="0.1" key={`scale-${prefill.loraScale}`} defaultValue={prefill.loraScale ?? 1} /></Field>
    </div></Form>{queued && <div className="vela-notice" role="status"><ImagePlus size={16} /><span>{queued}</span></div>}</Panel>
    <GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} />
  </Page>;
}

export function GenerationQueue({ items, error, loading }: { items: Row[]; error?: string; loading?: boolean }) {
  return <Panel title="Queue"><Notice error={error} loading={loading} empty={!loading && !items.length} />
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
  return <Page title="Queue" subtitle="One durable queue for Image, Eval, and Grid workflows."><GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} /></Page>;
}

function optionalNumber(value: FormDataEntryValue | null) { if (value == null || value === "") return undefined; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : undefined; }
