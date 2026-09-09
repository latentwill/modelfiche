import { createElement } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axisValues, compileGridPlan, parseGridAxisValues, type GridAxis, type GridAxisSchema } from "./grid";
import type { GenerationModel } from "./generation";
import { GridBoard, GridScreen, GridsScreen, gridTargetAxisEntry, intersectFieldSchemas, sortGridAxisEntries } from "./screens-grids";

const gridExportMocks = vi.hoisted(() => ({
  downloadGridImage: vi.fn(async (_options: unknown) => undefined),
}));
vi.mock("./grid-export", async () => ({
  ...await vi.importActual("./grid-export") as Record<string, unknown>,
  downloadGridImage: gridExportMocks.downloadGridImage,
}));

const model = (id: string, endpoint = "fal-a"): GenerationModel => ({ id, modelId: `family-${id}`, projectId: "project", name: `Model ${id}`,
  checkpointLabel: `step ${id}`, checkpointRevisionId: `revision-${id}`, provider: "fal", endpoint, providerModelUrl: `fal://${id}`, available: true });


afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("grid planner", () => {
  it("shows only Project before selection, then reveals the composer", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null), setItem: vi.fn(), removeItem: vi.fn() });
    vi.spyOn(globalThis, "fetch").mockImplementation(async request => {
      const url = typeof request === "string" ? request : String(request);
      const body = url.includes("/api/projects?") ? [{ id: "project-a", title: "Project A" }] : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    });
    render(createElement(GridsScreen, { params: new URLSearchParams() }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Project" })).toBeTruthy());
    expect(screen.queryByRole("button", { name: "X axis" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Y axis" })).toBeNull();
    expect(screen.queryByRole("listbox", { name: "Target axis targets" })).toBeNull();
    expect(screen.queryByText("Fixed prompt")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.click(await screen.findByRole("option", { name: "Project A" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "X axis" })).toBeTruthy());
  });

  it("renders project-only context, axis controls, and resets targets on project change", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null), setItem: vi.fn(), removeItem: vi.fn() });
    vi.spyOn(globalThis, "fetch").mockImplementation(async request => {
      const url = typeof request === "string" ? request : String(request);
      const body = url.includes("/api/projects?") ? [{ id: "project-a", title: "Project A" }, { id: "project-b", title: "Project B" }] : url.includes("project_id=project-a") ? [{ id: "version-a", project_id: "project-a", model_id: "family-a", name: "Model A", checkpoint_revision_id: "revision-a", fal_url: "fal://a", endpoint_id: "endpoint-a" }] : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    });
    render(createElement(GridsScreen, { params: new URLSearchParams("project=project-a&model_version=version-a") }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Project" })).toHaveTextContent("Project A"));
    expect(screen.getByRole("button", { name: "X axis" })).toBeTruthy();
    expect(screen.queryByText("Prompt set")).toBeNull();
  });

  it("parses endpoint-declared axis types, bounds, and options", () => {
    const integer: GridAxisSchema = { label: "Seed", type: "integer", parser: "integer", min: 1, max: 10, step: 1 };
    expect(parseGridAxisValues("1\n10", integer).map(entry => entry.value)).toEqual([1, 10]);
    expect(() => parseGridAxisValues("0", integer)).toThrow(/from 1 to 10/);
    expect(() => parseGridAxisValues("1.5", integer)).toThrow(/integer/);
    const toggle: GridAxisSchema = { label: "Expansion", type: "boolean", parser: "boolean" };
    expect(parseGridAxisValues("true\nfalse", toggle).map(entry => entry.value)).toEqual([true, false]);
    expect(() => parseGridAxisValues("sometimes", toggle)).toThrow(/true or false/);
    const preset: GridAxisSchema = { label: "Size", type: "enum", parser: "enum", options: ["square_hd", "portrait_4_3"] };
    expect(() => parseGridAxisValues("landscape_4_3", preset)).toThrow(/unsupported/);
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
    expect(preview.getAllByText("portrait")).toHaveLength(1);
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
      xLabel: "Prompt",
      yLabel: "Seed",
      imageHref: assetId => `#/gallery?asset=${assetId}`,
    }));
    const image = await review.findByRole("img");
    await waitFor(() => expect(image.getAttribute("src")).toBe("/delivery/full"));
    expect(image.closest(".grid-cell")).toBeTruthy();
    expect(review.getByRole("link", { name: "Open grid output in image viewer" })).toHaveAttribute("href", "#/gallery?asset=asset-1");
    expect(review.container.querySelector(".grid-board-completed")).toBeTruthy();
    expect(review.getByRole("table", { name: "Generated grid" })).toBeTruthy();
    expect(review.getAllByRole("columnheader").map(cell => cell.textContent)).toEqual(["Seed ↓ / Prompt →", "portrait"]);
    expect(review.getByRole("rowheader", { name: "7" })).toBeTruthy();
  });

  it("exports labeled compact and full-size images from the generated grid page", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue("workspace"), setItem: vi.fn(), removeItem: vi.fn() });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      id: "grid-1",
      name: "Illingworth comparison",
      status: "succeeded",
      plan: {
        axes: {
          x: { name: "target", values: [{ value: "illingworth-source" }] },
          y: { name: "prompt", values: [{ value: "portrait-case" }] },
          z: null,
        },
      },
      cells: [{
        status: "succeeded",
        x_index: 0,
        y_index: 0,
        coordinate: { x: 0, y: 0, z: null },
        asset_revision_id: "asset-1",
        model_version_name: "illingworth_krea2_4000_v4_000002500",
        model_version_source_kind: "training",
        checkpoint_step: 2500,
        target_snapshot: { target_id: "illingworth-source" },
        case_snapshot: { input: { prompt: "lngwr, a detailed portrait" } },
      }],
    }), { status: 200, headers: { "content-type": "application/json" } }));

    render(createElement(GridScreen, { id: "grid-1", projectId: "project-1" }));
    const small = await screen.findByRole("button", { name: "Export small" });
    const full = screen.getByRole("button", { name: "Export full size" });
    expect(small).toBeEnabled();
    expect(full).toBeEnabled();

    fireEvent.click(small);
    await waitFor(() => expect(gridExportMocks.downloadGridImage).toHaveBeenCalledTimes(1));
    const compact = gridExportMocks.downloadGridImage.mock.calls[0]?.[0] as {
      size: string;
      filename: string;
      slices: Array<{ cornerLabel: string; columns: Array<{ label: string; detail?: string }>; rows: Array<{ label: string }>; cells: Array<Array<{ assetRevisionId?: string }>> }>;
    };
    expect(compact).toMatchObject({
      size: "compact",
      filename: "illingworth-comparison-small.png",
      slices: [{
        cornerLabel: "prompt ↓ / Target →",
        columns: [{ label: "illingworth-source", detail: "Checkpoint: illingworth_krea2_4000_v4_000002500 · step 2500" }],
        rows: [{ label: "lngwr, a detailed portrait" }],
        cells: [[{ assetRevisionId: "asset-1" }]],
      }],
    });

    fireEvent.click(full);
    await waitFor(() => expect(gridExportMocks.downloadGridImage).toHaveBeenCalledTimes(2));
    expect(gridExportMocks.downloadGridImage.mock.calls[1]?.[0]).toMatchObject({
      size: "full",
      filename: "illingworth-comparison-full.png",
    });
  });

  it("labels target columns from their checkpoint provenance", () => {
    expect(gridTargetAxisEntry("fujiwara-source", 0, {
      target_snapshot: { target_id: "fujiwara-source" },
      model_version_name: "fujiwara-kaoru-krea2-v001",
      model_version_source_kind: "training",
      checkpoint_step: 3000,
    })).toMatchObject({
      label: "fujiwara-source",
      detail: "Checkpoint: fujiwara-kaoru-krea2-v001 · step 3000",
    });
    expect(gridTargetAxisEntry("weighted-60-40", 1, {
      target_snapshot: { target_id: "weighted-60-40" },
      model_version_name: "Fujiwara Kaoru 3000 × Hoover 3750 — weighted 60–40",
      model_version_source_kind: "checkpoint_merge",
      checkpoint_step: 3750,
    })).toMatchObject({
      label: "weighted-60-40",
      detail: "Merge output: Fujiwara Kaoru 3000 × Hoover 3750 — weighted 60–40",
    });
    expect(gridTargetAxisEntry("dawnjian-krea2-core-gestures", 2, {
      target_snapshot: { target_id: "dawnjian-krea2-core-gestures" },
      model_version_name: "dawnjian-krea2-core-gestures",
      model_version_source_kind: "training",
      checkpoint_step: 3500,
    })).toMatchObject({
      label: "dawnjian-krea2-core-gestures",
      detail: "Checkpoint: dawnjian-krea2-core-gestures · step 3500",
    });
  });

  it("sorts numeric checkpoint entries while preserving their coordinate indexes", () => {
    const sorted = sortGridAxisEntries([
      { value: "step-2750", label: "Step 2750", index: 0, sortValue: 2750 },
      { value: "step-1750", label: "Step 1750", index: 1, sortValue: 1750 },
      { value: "step-2500", label: "Step 2500", index: 2, sortValue: 2500 },
    ]);
    expect(sorted.map(entry => [entry.label, entry.index])).toEqual([
      ["Step 1750", 1],
      ["Step 2500", 2],
      ["Step 2750", 0],
    ]);
  });
});

it("intersects mixed endpoint option sets and bounds safely", () => {
  expect(intersectFieldSchemas([
    { type: "enum", parser: "enum", options: ["none", "regular", "high"] },
    { type: "enum", parser: "enum", options: ["none", "regular"] },
  ])).toMatchObject({ options: ["none", "regular"] });
  expect(intersectFieldSchemas([
    { type: "number", parser: "number", min: 0, max: 10 },
    { type: "number", parser: "number", min: 2, max: 8 },
  ])).toMatchObject({ min: 2, max: 8 });
  expect(intersectFieldSchemas([
    { type: "boolean", parser: "boolean" },
    { type: "number", parser: "number" },
  ])).toBeUndefined();
});
