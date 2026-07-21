import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OutputReviewControl, moveItem } from "./screens-evals";
import evalSource from "./screens-evals.tsx?raw";
import * as apiModule from "./api";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

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
    expect(evalSource).not.toContain('aria-label="Scope"');
    expect(evalSource).toContain('name="rating"');
    expect(evalSource).toContain('name="decision"');
    expect(evalSource).toContain("<select multiple value={visibleModels}");
  });

  it("uses bounded composite rating and decision keyboard controls", async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.spyOn(apiModule, "api").mockImplementation(async (_path, init) => {
      requests.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      return {};
    });
    render(<OutputReviewControl output={{ id: "output-a" }} />);
    const rating = screen.getByRole("radiogroup", { name: "Rating" });
    const oneStar = rating.querySelector('[role="radio"]') as HTMLElement;
    oneStar.focus();
    fireEvent.keyDown(oneStar, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("radio", { name: "2 stars" })).toHaveAttribute("aria-checked", "true"));
    const decision = screen.getByRole("radiogroup", { name: "Production decision" });
    const candidate = decision.querySelector('[role="radio"]') as HTMLElement;
    candidate.focus();
    fireEvent.keyDown(candidate, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("radio", { name: "Approved" })).toHaveAttribute("aria-checked", "true"));
    expect(requests.at(-1)).toMatchObject({ subject_type: "eval_output", subject_id: "output-a", rating: 2, decision: "approved" });
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
