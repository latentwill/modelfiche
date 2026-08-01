import { api, jsonBody, routeQuery, str } from "./api";

export type GenerationWorkflow = "image" | "eval" | "grid";
export type GenerationProviderId = "fal" | (string & {});
export type GenerationContext =
  | { kind: "project"; projectId: string }
  | { kind: "model"; projectId: string; modelId: string; modelVersionId?: string };

export type NormalizedGenerationParameters = {
  prompt: string;
  negativePrompt?: string;
  seed?: number;
  width?: number;
  height?: number;
  guidance?: number;
  steps?: number;
  sampler?: string;
  scheduler?: string;
  numImages: number;
  loraScale?: number;
  extra?: Record<string, unknown>;
};

export type GenerationModel = {
  id: string;
  modelId: string;
  projectId: string;
  checkpointId?: string;
  checkpointRevisionId?: string;
  name: string;
  baseModel?: string;
  checkpointLabel: string;
  provider?: GenerationProviderId;
  endpoint?: string;
  providerModelUrl?: string;
  available: boolean;
  unavailableReason?: string;
};
export type GenerationPickerMode = "single" | "multiple" | "fixed" | "axis";

/** Stable picker inputs shared by Image, Eval, and Grid workflows. */
export type GenerationPickerSelection = {
  mode: GenerationPickerMode;
  selectedIds: string[];
};

export function pickerModeMultiple(mode: GenerationPickerMode): boolean {
  return mode === "multiple" || mode === "axis";
}

export function eligibleGenerationModel(model: GenerationModel): boolean {
  return model.available && Boolean(model.id && model.modelId && model.projectId && model.checkpointRevisionId && model.provider && model.endpoint);
}

export function generationModelUnavailableReason(model: GenerationModel): string {
  if (!model.available) return model.unavailableReason ?? "Provider registration is unavailable.";
  if (!model.checkpointRevisionId) return "Checkpoint revision is missing.";
  if (!model.provider) return "Provider registration is missing.";
  if (!model.endpoint) return "Provider endpoint is missing.";
  if (!model.modelId || !model.projectId) return "Model project registration is incomplete.";
  return "";
}

export type NormalizedGenerationRequest = {
  workflow: GenerationWorkflow;
  context: GenerationContext;
  model: GenerationModel;
  parameters: NormalizedGenerationParameters;
  clientRequestId: string;
};

export type NormalizedGenerationStatus = "queued" | "running" | "completed" | "failed";
export type NormalizedGenerationResult = {
  provider: GenerationProviderId;
  providerRequestId?: string;
  status: NormalizedGenerationStatus;
  assetIds: string[];
  error?: string;
  raw?: unknown;
};
export type CompiledGenerationChild = { provider: GenerationProviderId; modelVersionId: string; compiledRequestId: string; admissionId: string };

export type CanonicalGeneratedAssetMetadata = {
  workflow: GenerationWorkflow;
  provider: GenerationProviderId;
  projectId: string;
  modelId: string;
  modelVersionId: string;
  checkpointId?: string;
  checkpointRevisionId: string;
  generatedAt?: string;
  parameters: NormalizedGenerationParameters;
  providerRequestId?: string;
};

/** Queue packets can persist this shape in GEN-07 without knowing provider payloads. */
export type GenerationQueueEnvelope = {
  id: string;
  request: NormalizedGenerationRequest;
  status: NormalizedGenerationStatus;
  result?: NormalizedGenerationResult;
};

export type QueueOwnership = "dam" | "provider";
export const generationQueueCapabilities = {
  fal: { owner: "dam" as QueueOwnership, dispatch: "internal" as const },
  comfyui: { owner: "provider" as QueueOwnership, dispatch: "provider_native" as const },
};

export interface GenerationProviderAdapter {
  readonly id: GenerationProviderId;
  resolveModel(row: Record<string, unknown>): GenerationModel;
  compile(request: NormalizedGenerationRequest): Promise<CompiledGenerationChild>;
  submit(request: NormalizedGenerationRequest): Promise<NormalizedGenerationResult>;
  normalizeStatus(raw: unknown): NormalizedGenerationStatus;
  normalizeResult(raw: unknown): NormalizedGenerationResult;
  canonicalMetadata(request: NormalizedGenerationRequest, result: NormalizedGenerationResult): CanonicalGeneratedAssetMetadata;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function endpointFrom(row: Record<string, unknown>) {
  const readiness = record(row.readiness);
  return str(row.endpoint_id ?? row.endpoint_adapter ?? readiness.endpoint_id ?? readiness.endpoint ?? readiness.fal_endpoint, "");
}

export const falAdapter: GenerationProviderAdapter = {
  id: "fal",
  resolveModel(row) {
    const readiness = record(row.readiness);
    const endpoint = endpointFrom(row);
    const providerModelUrl = str(row.fal_url ?? readiness.fal_url ?? readiness.fal_path, "");
    const projectId = str(row.project_id, "");
    const revision = str(row.checkpoint_revision_id, "");
    const available = Boolean(projectId && endpoint && providerModelUrl && revision);
    return {
      id: str(row.id, ""), modelId: str(row.model_id, ""), projectId,
      checkpointId: str(row.checkpoint_id, "") || undefined,
      checkpointRevisionId: revision || undefined,
      baseModel: str(row.base_model, "") || undefined,
      name: str(row.name ?? row.version, "Model version"),
      checkpointLabel: [str(row.filename, ""), row.checkpoint_step != null ? `step ${row.checkpoint_step}` : ""].filter(Boolean).join(" · ") || "Checkpoint",
      provider: providerModelUrl ? "fal" : undefined, endpoint: endpoint || undefined,
      providerModelUrl: providerModelUrl || undefined, available,
      unavailableReason: available ? undefined : !revision ? "No immutable checkpoint revision" : !providerModelUrl ? "Not registered with FAL" : !endpoint ? "No FAL endpoint registered" : "Model is outside the selected project",
    };
  },
  async compile(request) {
    const model = request.model;
    if (!model.available || !model.checkpointRevisionId || !model.endpoint) throw new Error(model.unavailableReason ?? "Model is unavailable for FAL generation.");
    if (model.projectId !== request.context.projectId) throw new Error("The selected checkpoint belongs to another project.");
    const p = request.parameters;
    const parameters: Record<string, unknown> = {
      endpoint_id: model.endpoint, lora_scale: p.loraScale ?? 1, num_images: p.numImages,
      seed: p.seed, image_size: p.width && p.height ? { width: p.width, height: p.height } : undefined,
      ...falEndpointParameters(model.endpoint, p), ...p.extra,
    };
    Object.keys(parameters).forEach(key => parameters[key] === undefined && delete parameters[key]);
    const projection = await api<Record<string, unknown>>("/api/generation-requests/compile", jsonBody({
      workflow: request.workflow, provider: "fal",
      context: { kind: request.context.kind, project_id: request.context.projectId, model_id: request.context.kind === "model" ? request.context.modelId : undefined },
      model_version_id: model.id, checkpoint_revision_id: model.checkpointRevisionId,
      prompt: p.prompt, parameters,
      client_request_id: request.clientRequestId,
    }));
    const admission = record(projection.admission);
    if (str(admission.state, "") !== "ready") throw new Error(`FAL admission is ${str(admission.state, "unavailable")}.`);
    return { provider: "fal", modelVersionId: model.id, compiledRequestId: request.clientRequestId, admissionId: str(admission.id) };
  },
  async submit(request) {
    const compiled = await falAdapter.compile(request);
    const queued = await api<Record<string, unknown>>("/api/generation-queue", jsonBody({
      workflow: request.workflow, provider: "fal",
      context: { kind: request.context.kind, project_id: request.context.projectId, model_id: request.context.kind === "model" ? request.context.modelId : undefined },
      model_version_ids: [request.model.id], compiled_request_id: request.clientRequestId,
      admission_id: compiled.admissionId, client_request_id: request.clientRequestId,
      request: { workflow: request.workflow, context: request.context, model: request.model, parameters: request.parameters },
    }));
    return { provider: "fal", providerRequestId: str(queued.id, "") || undefined, status: "queued", assetIds: [], raw: queued };
  },
  normalizeStatus(raw) {
    const state = str(record(raw).status ?? record(raw).state, "").toLowerCase();
    if (/complete|success|succeed/.test(state)) return "completed";
    if (/fail|error|cancel/.test(state)) return "failed";
    if (/run|process|submit/.test(state)) return "running";
    return "queued";
  },
  normalizeResult(raw) {
    const value = record(raw);
    const assets = Array.isArray(value.asset_ids) ? value.asset_ids.map(String) : [];
    return { provider: "fal", providerRequestId: str(value.request_id ?? value.job_id, "") || undefined, status: falAdapter.normalizeStatus(raw), assetIds: assets, error: str(value.error, "") || undefined, raw };
  },
  canonicalMetadata(request, result) {
    return { workflow: request.workflow, provider: "fal", projectId: request.context.projectId, modelId: request.model.modelId, modelVersionId: request.model.id, checkpointId: request.model.checkpointId, checkpointRevisionId: request.model.checkpointRevisionId!, parameters: request.parameters, providerRequestId: result.providerRequestId };
  },
};

export const generationProviders: Readonly<Record<string, GenerationProviderAdapter>> = { fal: falAdapter };

export function compileGenerationChild(request: NormalizedGenerationRequest) {
  const provider = request.model.provider ? generationProviders[request.model.provider] : undefined;
  if (!provider) throw new Error(request.model.unavailableReason ?? "The selected checkpoint has no installed generation provider.");
  return provider.compile(request);
}

export function generationModelFromRow(row: Record<string, unknown>): GenerationModel {
  // Registration discovery is intentionally isolated here; a ComfyUI adapter can own its own metadata.
  return falAdapter.resolveModel(row);
}

export function connectedFalGenerationModels(rows: Record<string, unknown>[], projectId: string, modelId?: string): GenerationModel[] {
  return rows
    .map(generationModelFromRow)
    .filter(model => model.projectId === projectId && model.provider === "fal" && eligibleGenerationModel(model) && (!modelId || model.modelId === modelId));
}

export function generatorHref(workflow: GenerationWorkflow, context: GenerationContext, prefillAssetId?: string) {
  const params: Record<string, unknown> = { workflow, project: context.projectId };
  if (context.kind === "model") {
    params.model = context.modelId;
    if (context.modelVersionId) params.model_version = context.modelVersionId;
  }
  if (prefillAssetId) params.prefill_asset = prefillAssetId;
  return `#/generate${routeQuery(params)}`;
}

export function loraScaleFromMetadata(metadata: Record<string, unknown>): number | undefined {
  const request = record(metadata.fal_request_input ?? metadata.provider_request_input);
  const parameters = record(metadata.parameters);
  const settings = record(metadata.generation_settings);
  const lora = record(metadata.lora);
  const requestLora = Array.isArray(request.loras) ? record(request.loras[0]) : {};
  const settingsLora = Array.isArray(settings.loras) ? record(settings.loras[0]) : {};
  return numberOrUndefined(
    metadata.lora_scale ?? lora.scale ?? parameters.lora_scale ?? request.lora_scale ??
    requestLora.scale ?? settings.lora_scale ?? settingsLora.scale,
  );
}

export function normalizedParametersFromMetadata(metadata: Record<string, unknown>): Partial<NormalizedGenerationParameters> {
  const request = record(metadata.fal_request_input ?? metadata.provider_request_input ?? metadata.generation_settings);
  const parameters = record(metadata.parameters);
  const size = record(request.image_size ?? parameters.image_size);
  return {
    prompt: str(metadata.prompt ?? metadata.caption ?? request.prompt, ""),
    negativePrompt: str(metadata.negative_prompt ?? request.negative_prompt, "") || undefined,
    seed: numberOrUndefined(metadata.seed ?? request.seed), width: numberOrUndefined(metadata.width ?? size.width), height: numberOrUndefined(metadata.height ?? size.height),
    guidance: numberOrUndefined(metadata.guidance ?? metadata.guidance_scale ?? request.guidance_scale),
    steps: numberOrUndefined(metadata.steps ?? metadata.num_inference_steps ?? request.num_inference_steps),

    sampler: str(metadata.sampler ?? request.sampler, "") || undefined, scheduler: str(metadata.scheduler ?? request.scheduler, "") || undefined,
    loraScale: loraScaleFromMetadata(metadata), numImages: 1,
  };
}
export type GenerationReplay = {
  context: Extract<GenerationContext, { kind: "model" }>;
  modelVersionId: string;
  parameters: Partial<NormalizedGenerationParameters>;
};

/** Returns a replay only when the image carries immutable model and provider evidence. */
export function generationReplayFromMetadata(metadata: Record<string, unknown>): GenerationReplay | null {
  const parameters = normalizedParametersFromMetadata(metadata);
  const projectId = str(metadata.project_id, "");
  const modelId = str(metadata.model_id, "");
  const modelVersionId = str(metadata.model_version_id, "");
  const checkpointRevisionId = str(metadata.checkpoint_revision_id, "");
  const provider = str(metadata.provider ?? metadata.provider_id, "");
  const endpoint = str(metadata.endpoint_id ?? metadata.endpoint ?? metadata.provider_endpoint, "");
  if (!projectId || !modelId || !modelVersionId || !checkpointRevisionId || !provider || !endpoint || !parameters.prompt) return null;
  return { context: { kind: "model", projectId, modelId, modelVersionId }, modelVersionId, parameters };
}

function numberOrUndefined(value: unknown) { const number = Number(value); return value === "" || value == null || !Number.isFinite(number) ? undefined : number; }

function falEndpointParameters(endpoint: string, parameters: NormalizedGenerationParameters): Record<string, unknown> {
  // Normalized fields are not blindly forwarded: each FAL adapter owns its accepted vocabulary.
  if (endpoint === "ideogram/v4/lora") return {};
  if (endpoint === "fal-ai/krea-2/turbo/lora") return {};
  return {};
}
