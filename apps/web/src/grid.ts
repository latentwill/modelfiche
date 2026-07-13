import type { GenerationModel, NormalizedGenerationParameters } from "./generation";

export type GridAxisField = "prompt" | "seed" | "guidance" | "steps" | "lora_scale" | "checkpoint_step" | "model";
export type GridAxisValue = { id: string; label: string; value: string | number };
export type GridAxis = { field: GridAxisField; values: GridAxisValue[] };
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
        const parameters: NormalizedGenerationParameters = { ...input.base, prompt: input.fixedPrompt?.trim() ?? "", seed: input.base.seed, numImages: 1 };
        for (const [axis, entry] of entries) {
          if (axis.field === "model" || axis.field === "checkpoint_step") {
            const selected = availableModels.find(candidate => candidate.id === String(entry.value));
            if (!selected) throw new Error(`Checkpoint ${entry.label} is outside the selected context.`);
            model = selected;
          } else if (axis.field === "prompt") parameters.prompt = String(entry.value);
          else if (axis.field === "seed") parameters.seed = numeric(entry, "Seed");
          else if (axis.field === "guidance") parameters.guidance = numeric(entry, "Guidance");
          else if (axis.field === "steps") parameters.steps = numeric(entry, "Steps");
          else if (axis.field === "lora_scale") parameters.loraScale = numeric(entry, "LoRA scale");
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
