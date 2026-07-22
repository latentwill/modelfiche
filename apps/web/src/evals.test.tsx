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

  it("uses project-only completed-run assessment creation without generation selectors", () => {
    const generator = evalSource.slice(evalSource.indexOf("export function EvalsScreen"), evalSource.indexOf("function EvalRunStrip"));
    expect(generator).toContain("Completed generation run");
    expect(generator).toContain("run_id: runId");
    expect(generator).toContain("assessment");
    expect(generator).not.toContain("/run");
    expect(generator).not.toContain("prompt_set");
    expect(generator).not.toContain("modelId");
    expect(generator).not.toContain("ModelCheckpointPicker");
  });

  it("removes Prompt Set and model selector controls from assessment flow", () => {
    expect(evalSource).not.toContain("aria-label=\"Saved set\"");
    expect(evalSource).not.toContain("prompt-sets");
    expect(evalSource).not.toContain("ModelCheckpointPicker");
    expect(evalSource).toContain("Completed generation run");
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
