import { describe, expect, it } from "vitest";
import { SHELL_LAYOUT_CONTRACT } from "./shell";

describe("shell rail layout", () => {
  it("keeps navigation scrollable independently from the docked footer", () => {
    expect(SHELL_LAYOUT_CONTRACT.navigation).toBe("scroll-region");
    expect(SHELL_LAYOUT_CONTRACT.footer).toBe("viewport-docked");
  });

  it("uses the dynamic viewport and keeps the inspector reachable", () => {
    expect(SHELL_LAYOUT_CONTRACT.inspector).toBe("scroll-region");
    expect(SHELL_LAYOUT_CONTRACT.viewport).toBe("100dvh");
  });
});
