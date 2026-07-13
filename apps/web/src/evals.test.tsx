import { describe, expect, it } from "vitest";
import { moveItem } from "./screens-evals";
import evalSource from "./screens-evals.tsx?raw";

describe("Eval workflow contracts", () => {
  it("preserves ordered inline prompts when reordering", () => {
    const prompts = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(moveItem(prompts, 2, 0).map(item => item.id)).toEqual(["c", "a", "b"]);
    expect(prompts.map(item => item.id)).toEqual(["a", "b", "c"]);
  });

  it("uses the shared model/checkpoint picker and Generate Eval action with square previews", () => {
    const generator = evalSource.slice(evalSource.indexOf("export function EvalsScreen"), evalSource.indexOf("export function EvalScreen"));
    expect(generator).toContain("Ordered manual prompts");
    expect(generator).toContain('<ModelCheckpointPicker mode="multiple" models={generationModels}');
    expect(evalSource).toContain('mode="single"');
    expect(evalSource).toContain('label="Model and checkpoint"');
    expect(evalSource).not.toContain("multiple={false}");
    expect(generator).toContain("submit=\"Generate Eval\"");
    expect(evalSource).toContain("className=\"comparison-image\"");
    expect(generator).toContain("children: compiled.map");
    for (const removed of ["Billing acknowledgement", "Model readiness", "Provider / endpoint", "Prompt Source", "Succeeded"])
      expect(generator).not.toContain(removed);
    const comparison = evalSource.slice(evalSource.indexOf("export function EvalComparisonScreen"), evalSource.indexOf("function RatingControl"));
    expect(comparison).toContain("Comparison filters");
    expect(comparison).toContain("Prompt contains");
    expect(comparison).toContain("Model checkpoints");
  });

  it("uses Dropdown for single-value evaluation controls while retaining listboxes", () => {
    expect(evalSource).not.toMatch(/<select(?![^>]*\bmultiple\b)/);
    expect(evalSource).toContain('aria-label="Saved set"');
    expect(evalSource).toContain('aria-label="Scope"');
    expect(evalSource).toContain('aria-label="Preset"');
    expect(evalSource).toContain('name="rating"');
    expect(evalSource).toContain('name="decision"');
    expect(evalSource).toContain("<select multiple value={visibleModels}");
    expect(evalSource).toContain("<select multiple size=");
    expect(evalSource).toContain('prompt: "Prompt"');
    expect(evalSource).toContain('checkpoint_step: "Model / checkpoint"');
    expect(evalSource).toContain("label: evalAxisLabels[axis] ?? axis");
    expect(evalSource).toContain("xLabel={evalAxisLabels[xField] ?? xField}");
    expect(evalSource).not.toContain("options={axes.map(axis => ({ value: axis, label: axis }))}");
  });

  it("keeps four ordered prompts distinct even when text is repeated or similar", () => {
    const prompts = [
      { prompt_id: "prompt-0", prompt: "same" },
      { prompt_id: "prompt-1", prompt: "similar" },
      { prompt_id: "prompt-2", prompt: "same" },
      { prompt_id: "prompt-3", prompt: "similar" },
    ];
    expect(prompts.map(item => item.prompt_id)).toEqual(["prompt-0", "prompt-1", "prompt-2", "prompt-3"]);
    expect(new Set(prompts.map(item => item.prompt_id)).size).toBe(4);
  });
});
