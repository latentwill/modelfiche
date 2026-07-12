import type { GenerationModel, NormalizedGenerationParameters } from "./generation";

export type GridAxisField = "prompt" | "seed" | "guidance" | "steps" | "lora_scale" | "checkpoint_step";
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
  return text.split("\n").map(value => value.trim()).filter(Boolean).map((value, index) => ({ id: `value-${index}`, label: value, value }));
}

export function compileGridPlan(input: {
  x: GridAxis;
  y: GridAxis;
  availableModels?: GenerationModel[];
  zModels?: GenerationModel[];
  defaultModel?: GenerationModel;
  fixedPrompt?: string;
  base: Omit<NormalizedGenerationParameters, "prompt" | "seed"> & { seed?: number };
}): GridCellPlan[] {
  if (input.x.field === input.y.field) throw new Error("X and Y axes must use different fields.");
  if (input.zModels?.length && (input.x.field === "checkpoint_step" || input.y.field === "checkpoint_step")) throw new Error("Checkpoint step cannot be an X or Y axis while the Z model axis is enabled.");
  if (!input.x.values.length) throw new Error("Add at least one X-axis value.");
  if (!input.y.values.length) throw new Error("Add at least one Y-axis value.");
  const promptAxis = input.x.field === "prompt" || input.y.field === "prompt";
  if (!promptAxis && !input.fixedPrompt?.trim()) throw new Error("Enter a prompt when Prompt is not an axis.");
  if (promptAxis && input.fixedPrompt?.trim()) throw new Error("Remove the fixed prompt while Prompt is an axis.");
  const zModels = input.zModels?.length ? input.zModels : input.defaultModel ? [input.defaultModel] : [];
  const availableModels = input.availableModels?.length ? input.availableModels : zModels;
  if (!zModels.length) throw new Error("Select an available model/checkpoint.");
  const cells: GridCellPlan[] = [];
  for (let zIndex = 0; zIndex < zModels.length; zIndex += 1) {
    const zModel = zModels[zIndex];
    for (let yIndex = 0; yIndex < input.y.values.length; yIndex += 1) {
      for (let xIndex = 0; xIndex < input.x.values.length; xIndex += 1) {
        const x = input.x.values[xIndex], y = input.y.values[yIndex];
        let model = zModel;
        const parameters: NormalizedGenerationParameters = { ...input.base, prompt: input.fixedPrompt?.trim() ?? "", seed: input.base.seed, numImages: 1 };
        for (const [axis, entry] of [[input.x, x], [input.y, y]] as const) {
          if (axis.field === "checkpoint_step") {
            const selected = availableModels.find(candidate => candidate.id === String(entry.value));
            if (!selected) throw new Error(`Checkpoint ${entry.label} is outside the selected context.`);
            model = selected;
          } else if (axis.field === "prompt") parameters.prompt = String(entry.value);
          else if (axis.field === "seed") parameters.seed = numeric(entry, "Seed");
          else if (axis.field === "guidance") parameters.guidance = numeric(entry, "Guidance");
          else if (axis.field === "steps") parameters.steps = numeric(entry, "Steps");
          else if (axis.field === "lora_scale") parameters.loraScale = numeric(entry, "LoRA scale");
        }
        cells.push({ id: `z${zIndex}-y${yIndex}-x${xIndex}`, ordinal: cells.length, xIndex, yIndex, zIndex, x, y,
          z: input.zModels?.length ? { id: zModel.id, label: `${zModel.name} · ${zModel.checkpointLabel}`, value: zModel.id } : undefined,
          model, parameters });
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
