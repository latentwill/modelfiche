import type { GenerationModel, NormalizedGenerationParameters } from "./generation";

export type GridAxisField = string;
export type GridAxisPrimitive = string | number | boolean | { width: number; height: number };
export type GridAxisValue = { id: string; label: string; value: GridAxisPrimitive };
export type GridAxis = { field: GridAxisField; values: GridAxisValue[] };
export type GridAxisSchema = {
  label?: string;
  type?: string;
  parser?: string;
  min?: number;
  max?: number;
  step?: number;
  max_length?: number;
  options?: unknown[];
  custom?: { min?: number; max?: number; step?: number };
  default_values?: unknown[];
};
export type GridCellPlan = {
  id: string;
  ordinal: number;
  xIndex: number;
  yIndex: number;
  zIndex: number;
  x: GridAxisValue;
  y: GridAxisValue;
  z?: GridAxisValue;
  model: GenerationModel;
  parameters: NormalizedGenerationParameters;
};

export function axisValues(text: string): GridAxisValue[] {
  return text.split(/\r?\n/).map(value => value.trim()).filter(Boolean).map((value, index) => ({
    id: `value-${index}`,
    label: value,
    value,
  }));
}

export function parseGridAxisValues(text: string, schema: GridAxisSchema): GridAxisValue[] {
  const parser = schema.parser ?? schema.type ?? "string";
  return text.split(/\r?\n/).map(value => value.trim()).filter(Boolean).map((label, index) => ({
    id: `value-${index}`,
    label,
    value: parseGridAxisValue(label, parser, schema),
  }));
}

function parseGridAxisValue(label: string, parser: string, schema: GridAxisSchema): GridAxisPrimitive {
  if (parser === "string") {
    if (schema.max_length !== undefined && label.length > schema.max_length) throw new Error(`${schema.label ?? "Value"} must be no longer than ${schema.max_length} characters.`);
    return label;
  }
  if (parser === "integer" || parser === "number") {
    const value = Number(label);
    if (!Number.isFinite(value) || parser === "integer" && !Number.isInteger(value)) throw new Error(`${schema.label ?? "Value"} “${label}” must be ${parser === "integer" ? "an integer" : "a number"}.`);
    if (schema.min !== undefined && value < schema.min || schema.max !== undefined && value > schema.max) throw new Error(`${schema.label ?? "Value"} “${label}” must be from ${schema.min ?? "−∞"} to ${schema.max ?? "∞"}.`);
    return value;
  }
  if (parser === "boolean") {
    if (!/^(true|false)$/i.test(label)) throw new Error(`${schema.label ?? "Value"} “${label}” must be true or false.`);
    return label.toLowerCase() === "true";
  }
  if (parser === "enum") {
    const options = (schema.options ?? []).map(String);
    if (!options.includes(label)) throw new Error(`${schema.label ?? "Value"} “${label}” is unsupported. Choose ${options.join(", ")}.`);
    return label;
  }
  if (parser === "image_size") {
    const options = (schema.options ?? []).map(String);
    if (options.includes(label)) return label;
    const match = /^(\d+)\s*[x×]\s*(\d+)$/i.exec(label);
    if (!match) throw new Error(`${schema.label ?? "Image size"} “${label}” must be an endpoint preset or WIDTH×HEIGHT.`);
    const width = Number(match[1]);
    const height = Number(match[2]);
    const custom = schema.custom ?? {};
    const minimum = custom.min ?? 1;
    const maximum = custom.max ?? Number.MAX_SAFE_INTEGER;
    const step = custom.step ?? 1;
    if ([width, height].some(value => value < minimum || value > maximum || value % step !== 0)) throw new Error(`${schema.label ?? "Image size"} “${label}” is outside endpoint bounds (${minimum}–${maximum}, step ${step}).`);
    return { width, height };
  }
  throw new Error(`${schema.label ?? "Axis"} uses unsupported parser “${parser}”.`);
}

/**
 * Compile a deterministic Cartesian grid. Axis entries are addressed by
 * indexes/IDs, never by their display labels, so repeated prompt text remains
 * distinct and output order is stable (Z, then Y, then X).
 */
export function compileGridPlan(input: {
  x: GridAxis;
  y: GridAxis;
  z?: GridAxis;
  availableModels?: GenerationModel[];
  zModels?: GenerationModel[];
  defaultModel?: GenerationModel;
  fixedPrompt?: string;
  base: Omit<NormalizedGenerationParameters, "prompt" | "seed"> & { seed?: number };
}): GridCellPlan[] {
  const axes = [input.x, input.y, ...(input.z ? [input.z] : [])];
  const usedFields = new Set<GridAxisField>();
  for (const axis of axes) {
    if (usedFields.has(axis.field)) throw new Error(`${axis.field} cannot be used by more than one axis.`);
    usedFields.add(axis.field);
    if (!axis.values.length) throw new Error(`Add at least one ${axis.field} axis value.`);
  }
  const modelAxis = axes.find(axis => axis.field === "model" || axis.field === "checkpoint_step");
  if (input.zModels?.length && (input.x.field === "checkpoint_step" || input.x.field === "model" || input.y.field === "checkpoint_step" || input.y.field === "model")) throw new Error("Checkpoint step cannot be an X or Y axis while the Z model axis is enabled.");
  const zModels = input.zModels?.length ? input.zModels : input.z?.field === "model" || input.z?.field === "checkpoint_step" ? undefined : input.defaultModel ? [input.defaultModel] : [];
  const availableModels = input.availableModels?.length ? input.availableModels : zModels ?? (input.defaultModel ? [input.defaultModel] : []);
  const promptAxis = axes.some(axis => axis.field === "prompt");
  if (!promptAxis && !input.fixedPrompt?.trim()) throw new Error("Enter a prompt when Prompt is not an axis.");
  if (promptAxis && input.fixedPrompt?.trim()) throw new Error("Remove the fixed prompt while Prompt is an axis.");
  if (!modelAxis && !input.defaultModel && !zModels?.length) throw new Error("Select a fixed model/checkpoint.");
  if (input.z?.field && input.z.field !== "model" && input.zModels?.length) throw new Error("Z model selections require Model as the Z axis.");
  const zValues = input.z?.values.length
    ? input.z.values
    : zModels?.length
      ? zModels.map(model => ({ id: model.id, label: `${model.name} · ${model.checkpointLabel}`, value: model.id }))
      : [{ id: "z-fixed", label: "Fixed", value: "fixed" }];
  const cells: GridCellPlan[] = [];
  for (let zIndex = 0; zIndex < zValues.length; zIndex += 1) {
    const z = zValues[zIndex];
    for (let yIndex = 0; yIndex < input.y.values.length; yIndex += 1) {
      const y = input.y.values[yIndex];
      for (let xIndex = 0; xIndex < input.x.values.length; xIndex += 1) {
        const x = input.x.values[xIndex];
        const entries = [[input.x, x], [input.y, y], ...(input.z ? [[input.z, z] as const] : [])] as Array<[GridAxis, GridAxisValue]>;
        let model = input.defaultModel ?? zModels?.[0];
        const parameters: NormalizedGenerationParameters = { ...input.base, extra: { ...(input.base.extra ?? {}) }, prompt: input.fixedPrompt?.trim() ?? "", seed: input.base.seed, numImages: 1 };
        for (const [axis, entry] of entries) {
          if (axis.field === "model" || axis.field === "checkpoint_step") {
            const selected = availableModels.find(candidate => candidate.id === String(entry.value));
            if (!selected) throw new Error(`Checkpoint ${entry.label} is outside the selected context.`);
            model = selected;
          } else if (axis.field === "prompt") parameters.prompt = String(entry.value);
          else if (axis.field === "seed") parameters.seed = numeric(entry, "Seed");
          else if (axis.field === "lora_scale") parameters.loraScale = numeric(entry, "LoRA scale");
          else parameters.extra = { ...(parameters.extra ?? {}), [axis.field]: entry.value };
        }
        if (!model) throw new Error("Select a fixed model/checkpoint or add a model axis.");
        cells.push({
          id: `z${zIndex}-y${yIndex}-x${xIndex}`,
          ordinal: cells.length,
          xIndex,
          yIndex,
          zIndex,
          x,
          y,
          z: input.z ? z : undefined,
          model,
          parameters,
        });
      }
    }
  }
  return cells;
}

function numeric(entry: GridAxisValue, label: string): number {
  const value = Number(entry.value);
  if (!Number.isFinite(value)) throw new Error(`${label} value “${entry.label}” must be a number.`);
  return value;
}
