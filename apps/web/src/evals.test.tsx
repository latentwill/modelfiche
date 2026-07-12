import { describe, expect, it } from "vitest";
import { moveItem } from "./screens-evals";

describe("Eval workflow contracts", () => {
  it("preserves ordered inline prompts when reordering", () => {
    const prompts = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(moveItem(prompts, 2, 0).map(item => item.id)).toEqual(["c", "a", "b"]);
    expect(prompts.map(item => item.id)).toEqual(["a", "b", "c"]);
  });
});
