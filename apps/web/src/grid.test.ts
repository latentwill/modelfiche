import { describe, expect, it } from "vitest";
import { axisValues, compileGridPlan, type GridAxis } from "./grid";
import type { GenerationModel } from "./generation";
import gridSource from "./screens-grids.tsx?raw";

const model = (id: string, endpoint = "fal-a"): GenerationModel => ({ id, modelId: `family-${id}`, projectId: "project", name: `Model ${id}`,
  checkpointLabel: `step ${id}`, checkpointRevisionId: `revision-${id}`, provider: "fal", endpoint, providerModelUrl: `fal://${id}`, available: true });

describe("grid planner", () => {
  it("uses the scoped shared workflow without legacy independent controls", () => {
    expect(gridSource).toContain("Generation context");
    expect(gridSource).toContain("<ModelCheckpointPicker");
    expect(gridSource).toContain('workflow: "grid"');
    expect(gridSource).toContain("Generate grid");
    expect(gridSource).toContain("children: compiled");
    for (const removed of ["Prompt source", "Num Images", "Acceleration vs Rendering Speed", "billing acknowledgement", "Provider / endpoint"])
      expect(gridSource).not.toContain(removed);
  });
  it("maps X prompts to columns and Y seeds to rows without colliding duplicate labels", () => {
    const cells = compileGridPlan({ x: { field: "prompt", values: axisValues("same\nsame") }, y: { field: "seed", values: axisValues("11\n22") },
      defaultModel: model("a"), availableModels: [model("a")], base: { numImages: 1 } });
    expect(cells).toHaveLength(4);
    expect(cells.map(cell => [cell.ordinal, cell.xIndex, cell.yIndex, cell.parameters.prompt, cell.parameters.seed])).toEqual([
      [0, 0, 0, "same", 11], [1, 1, 0, "same", 11], [2, 0, 1, "same", 22], [3, 1, 1, "same", 22],
    ]);
    expect(new Set(cells.map(cell => cell.id)).size).toBe(4);
  });

  it("uses checkpoint values as the model without multiplying the X/Y dimensions", () => {
    const a = model("a"), b = model("b", "fal-b");
    const checkpoint: GridAxis = { field: "checkpoint_step", values: [{ id: "a", label: "step a", value: a.id }, { id: "b", label: "step b", value: b.id }] };
    const cells = compileGridPlan({ x: checkpoint, y: { field: "seed", values: axisValues("1\n2") }, defaultModel: a, availableModels: [a, b], fixedPrompt: "fixed", base: { numImages: 1 } });
    expect(cells).toHaveLength(4);
    expect(cells.map(cell => cell.model.id)).toEqual(["a", "b", "a", "b"]);
    expect(cells.map(cell => cell.model.endpoint)).toEqual(["fal-a", "fal-b", "fal-a", "fal-b"]);
  });

  it("builds one ordered X/Y subgrid per Z checkpoint and rejects ambiguous checkpoint axes", () => {
    const a = model("a"), b = model("b");
    const input = { x: { field: "prompt", values: axisValues("one\ntwo") } as GridAxis, y: { field: "seed", values: axisValues("1\n2") } as GridAxis,
      zModels: [a, b], availableModels: [a, b], base: { numImages: 1 } };
    const cells = compileGridPlan(input);
    expect(cells).toHaveLength(8);
    expect(cells.map(cell => cell.zIndex)).toEqual([0, 0, 0, 0, 1, 1, 1, 1]);
    expect(() => compileGridPlan({ ...input, x: { field: "checkpoint_step", values: [{ id: "a", label: "a", value: "a" }] } })).toThrow(/cannot be an X or Y axis/);
  });
});
