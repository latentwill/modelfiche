import { createElement } from "react";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axisValues, compileGridPlan, type GridAxis } from "./grid";
import type { GenerationModel } from "./generation";
import { GridBoard } from "./screens-grids";
import gridSource from "./screens-grids.tsx?raw";

const model = (id: string, endpoint = "fal-a"): GenerationModel => ({ id, modelId: `family-${id}`, projectId: "project", name: `Model ${id}`,
  checkpointLabel: `step ${id}`, checkpointRevisionId: `revision-${id}`, provider: "fal", endpoint, providerModelUrl: `fal://${id}`, available: true });


afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("grid planner", () => {
  it("uses the fixed/global and ordered axis workflow without legacy Scope controls", () => {
    expect(gridSource).toContain("Fixed / global settings");
    expect(gridSource).toContain("X axis · columns");
    expect(gridSource).toContain("Y axis · rows");
    expect(gridSource).toContain("Z axis · optional subgrids");
    expect(gridSource).toContain("<ModelCheckpointPicker mode=\"axis\"");
    expect(gridSource).toContain("<ModelCheckpointPicker mode=\"fixed\"");
    expect(gridSource).toContain("Grid incomplete");
    expect(gridSource).toContain('workflow: "grid"');
    expect(gridSource).toContain("children: compiled");
    expect(gridSource).toContain('queue.error ? `/api/grids/${id}` : null');
    expect(gridSource).toContain("Historical · read only");
    expect(gridSource).not.toMatch(/\bScope\b/);
    for (const removed of ["Prompt source", "Num Images", "Acceleration vs Rendering Speed", "billing acknowledgement", "Provider / endpoint"])
      expect(gridSource).not.toContain(removed);
  });

  it("maps four prompts and two seeds to ordered X columns and Y rows with duplicate labels", () => {
    const cells = compileGridPlan({
      x: { field: "prompt", values: axisValues("same\nsimilar\nsame\nsimilar") },
      y: { field: "seed", values: axisValues("11\n22") },
      defaultModel: model("a"), availableModels: [model("a")], base: { numImages: 1 },
    });
    expect(cells).toHaveLength(8);
    expect(cells.map(cell => [cell.ordinal, cell.xIndex, cell.yIndex, cell.parameters.prompt, cell.parameters.seed])).toEqual([
      [0, 0, 0, "same", 11], [1, 1, 0, "similar", 11], [2, 2, 0, "same", 11], [3, 3, 0, "similar", 11],
      [4, 0, 1, "same", 22], [5, 1, 1, "similar", 22], [6, 2, 1, "same", 22], [7, 3, 1, "similar", 22],
    ]);
    expect(new Set(cells.map(cell => cell.id)).size).toBe(8);
  });

  it("rejects fixed parameters when their field is assigned to an axis", () => {
    expect(() => compileGridPlan({ x: { field: "prompt", values: axisValues("one\ntwo") }, y: { field: "seed", values: axisValues("1") }, fixedPrompt: "should not be fixed", defaultModel: model("a"), base: { numImages: 1 } })).toThrow(/Remove the fixed prompt/);
    expect(() => compileGridPlan({ x: { field: "seed", values: axisValues("1") }, y: { field: "seed", values: axisValues("2") }, defaultModel: model("a"), base: { numImages: 1 } })).toThrow(/more than one axis/);
  });

  it("uses model/checkpoint axis IDs without display-text lookup", () => {
    const a = model("a"), b = model("b");
    const cells = compileGridPlan({
      x: { field: "model", values: [{ id: "stable-a", label: "Same label", value: a.id }, { id: "stable-b", label: "Same label", value: b.id }] },
      y: { field: "seed", values: axisValues("1") }, availableModels: [a, b], fixedPrompt: "fixed", base: { numImages: 1 },
    });
    expect(cells.map(cell => cell.model.id)).toEqual(["a", "b"]);
    expect(cells.map(cell => cell.x.id)).toEqual(["stable-a", "stable-b"]);
  });

  it("uses checkpoint values as the model without multiplying the X/Y dimensions", () => {
    const a = model("a"), b = model("b", "fal-b");
    const checkpoint: GridAxis = { field: "checkpoint_step", values: [{ id: "a", label: "step a", value: a.id }, { id: "b", label: "step b", value: b.id }] };
    const cells = compileGridPlan({ x: checkpoint, y: { field: "seed", values: axisValues("1\n2") }, availableModels: [a, b], fixedPrompt: "fixed", base: { numImages: 1 } });
    expect(cells).toHaveLength(4);
    expect(cells.map(cell => cell.model.id)).toEqual(["a", "b", "a", "b"]);
    expect(cells.map(cell => cell.model.endpoint)).toEqual(["fal-a", "fal-b", "fal-a", "fal-b"]);
  });

  it("builds one ordered X/Y subgrid per Z checkpoint and rejects ambiguous checkpoint axes", () => {
    const a = model("a"), b = model("b");
    const input = { x: { field: "prompt", values: axisValues("one\ntwo") } as GridAxis, y: { field: "seed", values: axisValues("1\n2") } as GridAxis,
      zModels: [a, b], availableModels: [a, b], base: { numImages: 1 }, fixedPrompt: undefined };
    const cells = compileGridPlan(input);
    expect(cells).toHaveLength(8);
    expect(cells.map(cell => cell.zIndex)).toEqual([0, 0, 0, 0, 1, 1, 1, 1]);
    expect(() => compileGridPlan({ ...input, x: { field: "checkpoint_step", values: [{ id: "a", label: "a", value: "a" }] } })).toThrow(/cannot be an X or Y axis/);
  });

  it("shows planned cell content and contains completed images in review cells", async () => {
    const x = { field: "prompt", values: axisValues("portrait") } as GridAxis;
    const y = { field: "seed", values: axisValues("7") } as GridAxis;
    const cells = compileGridPlan({ x, y, defaultModel: model("a"), availableModels: [model("a")], base: { numImages: 1 } });
    const preview = render(createElement(GridBoard, { x: x.values, y: y.values, cells }));
    expect(preview.getAllByText("portrait")).toHaveLength(2);
    expect(preview.container.querySelector(".grid-board-preview")).toBeTruthy();

    cleanup();
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => null } });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      kind: "descriptor",
      descriptor: { delivery_url: "/delivery/full", asset_revision_id: "asset-1", variant: { kind: "thumbnail" } },
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const review = render(createElement(GridBoard, {
      x: x.values,
      y: y.values,
      cells: [{ x_index: 0, y_index: 0, asset_revision_id: "asset-1" }],
      completed: true,
    }));
    const image = await review.findByRole("img");
    await waitFor(() => expect(image.getAttribute("src")).toBe("/delivery/full"));
    expect(image.parentElement?.classList.contains("grid-cell")).toBe(true);
    expect(review.container.querySelector(".grid-board-completed")).toBeTruthy();
  });
});
