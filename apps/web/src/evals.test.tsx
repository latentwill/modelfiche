import { describe, expect, it } from "vitest";
import { moveItem } from "./screens-evals";
import evalSource from "./screens-evals.tsx?raw";

describe("Eval workflow contracts", () => {
  it("preserves ordered inline prompts when reordering", () => {
    const prompts = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(moveItem(prompts, 2, 0).map(item => item.id)).toEqual(["c", "a", "b"]);
    expect(prompts.map(item => item.id)).toEqual(["a", "b", "c"]);
  });

  it("exposes the scoped comparison workflow without legacy Eval controls", () => {
    const generator = evalSource.slice(evalSource.indexOf("export function EvalsScreen"), evalSource.indexOf("function EndpointFields"));
    expect(generator).toContain("Ordered manual prompts");
    expect(generator).toContain("<ModelCheckpointPicker models={generationModels}");
    expect(generator).toContain("children: compiled.map");
    for (const removed of ["Billing acknowledgement", "Model readiness", "Provider / endpoint", "Prompt Source", "Succeeded"])
      expect(generator).not.toContain(removed);
    const comparison = evalSource.slice(evalSource.indexOf("export function EvalComparisonScreen"), evalSource.indexOf("function RatingControl"));
    expect(comparison).toContain("Comparison filters");
    expect(comparison).toContain("Prompt contains");
    expect(comparison).toContain("Model checkpoints");
  });
});
