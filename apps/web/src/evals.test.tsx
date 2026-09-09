import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { evalOutputProvenanceLabel, OutputReviewControl, moveItem } from "./screens-evals";
import * as apiModule from "./api";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Eval workflow contracts", () => {
  it("preserves ordered inline prompts when reordering", () => {
    const prompts = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(moveItem(prompts, 2, 0).map(item => item.id)).toEqual(["c", "a", "b"]);
    expect(prompts.map(item => item.id)).toEqual(["a", "b", "c"]);
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
  it("labels source checkpoints and merge targets from canonical provenance", () => {
    expect(evalOutputProvenanceLabel({
      provenance: {
        kind: "checkpoint",
        axis_values: { target: "fujiwara-source" },
        model_name: "fujiwara-kaoru-krea2-v001",
        checkpoint_step: 3000,
        training_run_name: "fujiwara training",
      },
    })).toEqual({
      title: "Target · fujiwara-source",
      detail: "fujiwara-kaoru-krea2-v001 · step 3000 · run fujiwara training",
      kind: "checkpoint",
    });
    expect(evalOutputProvenanceLabel({
      provenance: {
        kind: "merge",
        axis_values: { target: "cosine-anchor" },
        model_name: "Fujiwara Kaoru × Hoover — cosine anchor",
        merge_notation: "cosine-gated[r64] { anchor=fkaor@3000; donor=qhoov@3750 }",
      },
    })).toEqual({
      title: "Target · cosine-anchor",
      detail: "Merge · cosine-gated[r64] { anchor=fkaor@3000; donor=qhoov@3750 }",
      kind: "merge",
    });
  });

});
