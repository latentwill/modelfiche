import { describe, expect, it } from "vitest";

import { PALETTE, isDitherColor } from "./palette";

describe("dither palette", () => {
  it("limits chart hues to blue-green variants and a neutral no-data seed", () => {
    expect(Object.keys(PALETTE).sort()).toEqual(["blue", "cyan", "green", "grey", "mint", "teal"]);
    expect(["purple", "pink", "orange", "red"].every(color => !isDitherColor(color))).toBe(true);
  });
});
