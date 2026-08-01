import { useEffect, useMemo, useRef, useState } from "react";
import { ImagePlus } from "lucide-react";
import { api, idOf, jsonBody, listOf, routeQuery, str } from "./api";
import { AssetImage } from "./asset-image";
import { pollWhileActive, useResource } from "./hooks";
import { Dropdown, Field, Form, Notice, Page, Panel, Status } from "./ui";
import { falAdapter, generationModelFromRow, generationModelUnavailableReason, pickerModeMultiple, normalizedParametersFromMetadata, type GenerationContext, type GenerationModel, type GenerationPickerMode, type NormalizedGenerationParameters } from "./generation";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const PROGRESS_POLL = { pollInterval: 1000, pollWhile: pollWhileActive };


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

export function EndpointFields({ schema, omit = [], onChange }: { schema?: Row; omit?: string[]; onChange?: () => void }) {
  const defaults = (schema?.defaults ?? {}) as Row;
  const fieldSchema = (schema?.fields ?? {}) as Record<string, Row>;
  const requestFields = Array.isArray(schema?.request_fields) ? schema.request_fields.map(String) : [];
  const omitted = new Set(omit);
  const fields = Array.from(new Set([...Object.keys(defaults), ...requestFields])).filter(key => !endpointFieldExclusions[key] && !omitted.has(key));
  return <>{fields.map(key => <EndpointField key={key} field={key} value={defaults[key]} schema={fieldSchema[key]} onChange={onChange} />)}</>;
}

function EndpointField({ field, value, schema = {}, onChange }: { field: string; value: unknown; schema?: Row; onChange?: () => void }) {
  const label = field === "image_size" ? "Aspect ratio / resolution" : field.replaceAll("_", " ");
  const type = str(schema.type, typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "string");
  const options = Array.isArray(schema.options) ? schema.options.map(String) : [];
  const [imageSize, setImageSize] = useState(typeof value === "object" ? "custom" : str(value, options[0]));
  if (type === "boolean") return <Field label={label}><Dropdown aria-label={label} name={field} defaultValue={String(value ?? false)} onChange={onChange} options={[{ value: "true", label: "Enabled" }, { value: "false", label: "Disabled" }]} /></Field>;
  if (type === "image_size") {
    const custom = (schema.custom ?? {}) as Row;
    const current = typeof value === "object" && value ? value as Row : {};
    return <><Field label={label} hint="Presets come from the selected FAL endpoint. Use custom dimensions only when exact pixels are required."><Dropdown aria-label={label} name={field} value={imageSize} onChange={next => { setImageSize(next); onChange?.(); }} options={[...options.map(option => ({ value: option, label: imageSizeLabels[option] ?? option.replaceAll("_", " ") })), { value: "custom", label: "Custom dimensions" }]} /></Field>{imageSize === "custom" && <><Field label="Custom width"><input name={`${field}_width`} type="number" min={Number(custom.min)} max={Number(custom.max)} step={Number(custom.step)} defaultValue={current.width === undefined ? undefined : Number(current.width)} onChange={onChange} required /></Field><Field label="Custom height"><input name={`${field}_height`} type="number" min={Number(custom.min)} max={Number(custom.max)} step={Number(custom.step)} defaultValue={current.height === undefined ? undefined : Number(current.height)} onChange={onChange} required /></Field></>}
    </>;
  }
  if (type === "enum" && options.length) return <Field label={label}><Dropdown aria-label={label} name={field} defaultValue={str(value, options[0])} onChange={onChange} options={options.map(option => ({ value: option, label: option }))} /></Field>;
  if (type === "integer" || type === "number") return <Field label={label}><input name={field} type="number" min={schema.min === undefined ? undefined : Number(schema.min)} max={schema.max === undefined ? undefined : Number(schema.max)} step={schema.step === undefined ? (type === "integer" ? 1 : "any") : Number(schema.step)} defaultValue={value === undefined ? "" : Number(value)} onChange={onChange} /></Field>;
  return <Field label={label}><input name={field} defaultValue={str(value, "")} onChange={onChange} /></Field>;
}
export function LoraScaleField({ schema, defaultValue, onChange }: { schema?: Row; defaultValue?: number; onChange?: () => void }) {
  const grid = schema?.grid && typeof schema.grid === "object" ? schema.grid as Row : {};
  const axisFields = grid.axis_fields && typeof grid.axis_fields === "object" ? grid.axis_fields as Record<string, Row> : {};
  const field = axisFields.lora_scale;
  if (!field) return <Field label="LoRA scale" hint="The selected endpoint does not publish a LoRA scale contract."><input aria-label="LoRA scale" disabled /></Field>;
  return <Field label={str(field.label, "LoRA scale")}><input
    name="lora_scale"
    type="number"
    min={field.min === undefined ? undefined : Number(field.min)}
    max={field.max === undefined ? undefined : Number(field.max)}
    step={field.step === undefined ? undefined : Number(field.step)}
    defaultValue={defaultValue ?? (field.default === undefined ? undefined : Number(field.default))}
    onChange={onChange}
    required
  /></Field>;
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

export function generationActivityTransition(previous: Record<string, string>, items: Row[]) {
  const statuses: Record<string, string> = {};
  let changed = false;
  for (const item of items) {
    const itemId = idOf(item);
    const status = str(item.status).toLowerCase();
    statuses[itemId] = status;
    if (previous[itemId] && !/completed|succeeded|failed|cancel/.test(previous[itemId]) && /completed|succeeded/.test(status)) changed = true;
  }
  return { statuses, changed };
}

export function ImageGeneratorScreen({ params }: { params: URLSearchParams }) {
  const initialProject = params.get("project") ?? "";
  const initialModel = params.get("model") ?? "";
  const initialVersion = params.get("model_version") ?? "";
  const prefillAssetId = params.get("prefill_asset") ?? "";
  const projects = useResource<unknown>("/api/projects?limit=100");
  const [projectId, setProjectId] = useState(initialProject);
  const [modelId, setModelId] = useState(initialModel);
  const [selected, setSelected] = useState<string[]>(initialVersion ? [initialVersion] : []);
  const modelFamilies = useResource<unknown>(projectId ? `/api/models?project_id=${encodeURIComponent(projectId)}` : null);
  const versions = useResource<unknown>(projectId ? `/api/model-versions?project_id=${encodeURIComponent(projectId)}` : null);
  const endpoints = useResource<unknown>("/api/eval-endpoints");
  const assetContext = useResource<Row>(prefillAssetId ? `/api/assets/${prefillAssetId}/context` : null);
  const [prefill, setPrefill] = useState<Partial<NormalizedGenerationParameters>>({ numImages: 1 });
  const [queued, setQueued] = useState("");
  const [endpointId, setEndpointId] = useState("");
  const [promptValue, setPromptValue] = useState("");
  const [requestRevision, setRequestRevision] = useState(0);
  const [requestValid, setRequestValid] = useState(false);
  const requestFieldsRef = useRef<HTMLDivElement>(null);
  const queueStatuses = useRef<Record<string, string>>({});
  const queue = useResource<unknown>("/api/generation-queue", PROGRESS_POLL);
  const generationModels = useMemo(() => rows(versions.data).map(generationModelFromRow).filter(model => model.projectId === projectId && (!modelId || model.modelId === modelId)), [versions.data, projectId, modelId]);
  useEffect(() => {
    if (!assetContext.data) return;
    const metadata = assetContext.data.metadata && typeof assetContext.data.metadata === "object" ? assetContext.data.metadata as Row : {};
    const asset = assetContext.data.asset && typeof assetContext.data.asset === "object" ? assetContext.data.asset as Row : {};
    const enriched = { ...asset, ...assetContext.data, ...metadata };
    setPrefill(normalizedParametersFromMetadata(enriched));
    setPromptValue(str(enriched.prompt, ""));
    const versionId = str(enriched.model_version_id, "");
    const enrichedProject = str(enriched.project_id, "");
    const enrichedModel = str(enriched.model_id, "");
    if (!initialProject && enrichedProject) setProjectId(enrichedProject);
    if (!initialModel && enrichedModel) setModelId(enrichedModel);
    if (versionId) setSelected([versionId]);
  }, [assetContext.data]);
  // Do not clear route/replay IDs while the real version registration is loading.
  useEffect(() => {
    if (versions.data == null) return;
    setSelected(ids => ids.filter(id => generationModels.some(model => model.id === id)));
  }, [generationModels, versions.data]);
  const context: GenerationContext | null = projectId ? modelId ? { kind: "model", projectId, modelId, modelVersionId: selected[0] } : { kind: "project", projectId } : null;
  const selectedModel = generationModels.find(model => model.id === selected[0]);
  const compatibleEndpoints = useMemo(() => {
    if (!selectedModel) return [];
    const baseModel = (selectedModel.baseModel ?? "").toLowerCase().replaceAll("_", "-");
    return rows(endpoints.data).filter(endpoint => {
      const id = str(endpoint.endpoint_id ?? endpoint.id, "");
      const markers = Array.isArray(endpoint.compatible_base_model_markers) ? endpoint.compatible_base_model_markers.map(value => String(value).toLowerCase()) : [];
      return id === selectedModel.endpoint || markers.some(marker => baseModel.includes(marker));
    });
  }, [endpoints.data, selectedModel]);
  useEffect(() => {
    if (!selectedModel) { setEndpointId(""); return; }
    setEndpointId(current => compatibleEndpoints.some(endpoint => str(endpoint.endpoint_id ?? endpoint.id) === current) ? current : selectedModel.endpoint ?? "");
  }, [compatibleEndpoints, selectedModel]);
  useEffect(() => {
    const transition = generationActivityTransition(queueStatuses.current, rows(queue.data));
    queueStatuses.current = transition.statuses;
    if (transition.changed) window.dispatchEvent(new Event("modelfiche:activity-changed"));
  }, [queue.data]);
  const endpointSchema = useResource<Row>(endpointId ? `/api/eval-endpoints/${endpointId}/schema` : null);
  const imageSchema = useMemo(() => {
    if (!endpointSchema.data) return undefined;
    const defaults = (endpointSchema.data.defaults ?? {}) as Row;
    const requestFields = Array.isArray(endpointSchema.data.request_fields) ? endpointSchema.data.request_fields.map(String) : [];
    const supported = new Set([...Object.keys(defaults), ...requestFields]);
    const replayDefaults: Row = {
      image_size: prefill.width && prefill.height ? { width: prefill.width, height: prefill.height } : undefined,
      negative_prompt: prefill.negativePrompt,
      guidance: prefill.guidance,
      guidance_scale: prefill.guidance,
      steps: prefill.steps,
      num_inference_steps: prefill.steps,
      sampler: prefill.sampler,
      scheduler: prefill.scheduler,
    };
    const overrides = Object.fromEntries(Object.entries(replayDefaults).filter(([key, value]) => value !== undefined && supported.has(key)));
    return { ...endpointSchema.data, defaults: { ...defaults, ...overrides } };
  }, [endpointSchema.data, prefill.width, prefill.height, prefill.negativePrompt, prefill.guidance, prefill.steps, prefill.sampler, prefill.scheduler]);
  useEffect(() => {
    setRequestValid(Boolean(requestFieldsRef.current?.closest("form")?.checkValidity()));
  }, [imageSchema, promptValue, requestRevision]);
  const configurationError = !projectId ? "Select a project." : !selectedModel ? "Select an available model checkpoint." : !selectedModel.available ? selectedModel.unavailableReason || "Select a provider-registered checkpoint." : !endpointId ? "Select a compatible FAL endpoint." : endpointSchema.loading || !endpointSchema.data ? "Loading settings for the selected FAL endpoint." : !promptValue.trim() ? "Enter a prompt." : !requestValid ? "Complete the endpoint-defined fields within their allowed bounds." : "";
  const imagePreflightError = configurationError;
  return <Page title="Image Generator" subtitle="Generate through the shared provider layer using the selected project/model context.">
    <Notice error={projects.error || modelFamilies.error || versions.error || endpoints.error || assetContext.error || endpointSchema.error} loading={projects.loading || modelFamilies.loading || versions.loading || endpoints.loading || assetContext.loading} />
    {prefillAssetId && <div className="generation-source-layout"><figure className="generation-image-frame"><AssetImage assetRevisionId={prefillAssetId} variant="content" alt="Source image for generation settings" /><figcaption>Source image · settings reference</figcaption></figure><div className="vela-notice" role="status"><strong>Settings loaded</strong><span>Supported settings from image {prefillAssetId} are prefilled. Nothing will run until you submit.</span></div></div>}
    <Panel title="Generation context"><div className="form-grid">
      <Field label="Project"><Dropdown aria-label="Project" value={projectId} options={[{ value: "", label: "Select..." }, ...rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))]} onChange={value => { setProjectId(value); setModelId(""); setSelected([]);  }} /></Field>
      <Field label="Model"><Dropdown aria-label="Model" value={modelId} disabled={!projectId} options={[{ value: "", label: "All models" }, ...rows(modelFamilies.data).map(model => ({ value: idOf(model), label: str(model.name) }))]} onChange={value => { setModelId(value); setSelected([]);  }} /></Field>
    </div>
    <ModelCheckpointPicker mode="single" models={generationModels} selected={selected} onChange={ids => { setSelected(ids);  }} label="Model / checkpoint" />
    <Field label="FAL endpoint"><Dropdown aria-label="FAL endpoint" value={endpointId} disabled={!selectedModel} options={[{ value: "", label: "Select..." }, ...compatibleEndpoints.map(endpoint => ({ value: str(endpoint.endpoint_id ?? endpoint.id), label: str(endpoint.name, str(endpoint.endpoint_id ?? endpoint.id)) }))]} onChange={value => { setEndpointId(value);  }} /></Field>
    {context && selected[0] && <p className="vela-meta">Provider and endpoint: {selectedModel?.provider?.toUpperCase()} / {endpointId}</p>}
    </Panel>
    <div className={`vela-notice${imagePreflightError ? " vela-notice-error" : ""}`} role="status"><strong>Generation preflight</strong><span>{imagePreflightError || `Ready for ${selectedModel?.provider?.toUpperCase()} / ${endpointId}. Enter a prompt, then generate one image.`}</span></div>
    <Panel title="Image request"><Form submit="Generate image" submitDisabled={Boolean(imagePreflightError)} onSubmit={async form => {
      if (!context) throw new Error("Select a project context.");
      if (imagePreflightError) throw new Error(imagePreflightError);
      const model = generationModels.find(item => item.id === selected[0]);
      if (!model) throw new Error("Select an available model checkpoint.");
      const endpointParameters = parametersFromForm(form, imageSchema);
      const parameters: NormalizedGenerationParameters = { prompt: String(form.get("prompt") ?? ""), seed: optionalNumber(form.get("seed")), loraScale: optionalNumber(form.get("lora_scale")), numImages: 1, extra: endpointParameters };
      const result = await falAdapter.submit({ workflow: "image", context, model: { ...model, endpoint: endpointId }, parameters, clientRequestId: crypto.randomUUID() });
      setQueued(result.providerRequestId ? `Queued as ${result.providerRequestId}.` : "Generation queued."); await queue.reload();
    }} repeatable><div ref={requestFieldsRef}><div className="form-grid">
      <Field label="Prompt"><textarea name="prompt" required value={promptValue} onChange={event => { setPromptValue(event.target.value); setRequestRevision(value => value + 1);  }} /></Field>
      <Field label="Seed"><input name="seed" type="number" key={`seed-${prefill.seed}`} defaultValue={prefill.seed} onChange={() => { setRequestRevision(value => value + 1);  }} /></Field>
      <EndpointFields key={`${endpointId}-${prefill.width}-${prefill.height}`} schema={imageSchema} omit={["num_images", "seed"]} onChange={() => { setRequestRevision(value => value + 1);  }} />
      <LoraScaleField key={`scale-${endpointId}-${prefill.loraScale}`} schema={imageSchema} defaultValue={prefill.loraScale} onChange={() => { setRequestRevision(value => value + 1);  }} />
    </div>
    </div>
    </Form>{queued && <div className="vela-notice" role="status"><ImagePlus size={16} /><span>{queued}</span></div>}</Panel>
    <GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} />
  </Page>;
}

export function formatQueueTimestamp(value: unknown) {
  const date = new Date(str(value, ""));
  return Number.isNaN(date.valueOf()) ? "Time unavailable" : date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}


export function GenerationQueue({ items, error, loading, onRetry }: { items: Row[]; error?: string; loading?: boolean; onRetry?: () => Promise<unknown> | unknown }) {
  const [retrying, setRetrying] = useState("");
  const [retryError, setRetryError] = useState("");
  const visibleItems = [...items].reverse();
  async function retry(item: Row) {
    setRetrying(idOf(item)); setRetryError("");
    try {
      await api(`/api/generation-queue/${idOf(item)}/retry`, jsonBody({}));
      await onRetry?.();
    } catch (reason) {
      setRetryError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRetrying("");
    }
  }
  return <Panel title="Queue"><Notice error={retryError || error} loading={loading} empty={!loading && !items.length} />
    {!!visibleItems.length && <div className="table-wrap"><table><thead><tr><th>Submitted</th><th>Workflow</th><th>Context</th><th>Models</th><th>Status</th><th>Progress / result</th>{onRetry && <th>Recovery</th>}</tr></thead><tbody>{visibleItems.map(item => {
      const progress = item.progress && typeof item.progress === "object" ? item.progress as Row : {};
      const request = item.request && typeof item.request === "object" ? item.request as Row : {};
      const context = request.context && typeof request.context === "object" ? request.context as Row : {};
      const model = request.model && typeof request.model === "object" ? request.model as Row : {};
      const modelIds = Array.isArray(item.model_version_ids) ? item.model_version_ids.map(String) : [];
      const projectLabel = str(context.projectTitle ?? context.projectName, "Project unavailable");
      const modelLabel = str(model.name, "");
      const checkpointLabel = str(model.checkpointLabel, "");
      const workflow = str(item.workflow);
      const failed = str(item.status).toLowerCase() === "failed";
      const workflowLink = workflow === "eval" ? <a href={`#/eval-comparison/${idOf(item)}${routeQuery({ project: item.project_id })}`}>Eval</a> : workflow === "grid" ? <a href={`#/grid/${encodeURIComponent(str(item.grid_definition_id))}${routeQuery({ project: item.project_id })}`}>Grid</a> : workflow;
      return <tr key={idOf(item)}><td><time dateTime={str(item.created_at, "")}>{formatQueueTimestamp(item.created_at)}</time></td><td>{workflowLink}</td><td>{projectLabel}{modelLabel ? ` / ${modelLabel}` : ""}</td><td>{checkpointLabel || `${modelIds.length} model${modelIds.length === 1 ? "" : "s"}`}</td><td><Status value={str(item.status)} /></td><td>{item.error ? str(item.error) : `${str(progress.completed, "0")} / ${str(progress.total, "-")}`}</td>{onRetry && <td>{failed ? <button type="button" disabled={Boolean(retrying)} onClick={() => void retry(item)}>{retrying === idOf(item) ? "Retrying…" : "Retry failed work"}</button> : "—"}</td>}</tr>;
    })}</tbody></table></div>}
  </Panel>;
}

export function GenerationQueueScreen() {
  const queue = useResource<unknown>("/api/generation-queue", PROGRESS_POLL);
  return <Page title="Queue" subtitle="One durable queue for Image, Eval, and Grid workflows."><GenerationQueue items={rows(queue.data)} error={queue.error} loading={queue.loading} onRetry={queue.reload} /></Page>;
}

function optionalNumber(value: FormDataEntryValue | null) { if (value == null || value === "") return undefined; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : undefined; }
