import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api", () => {
  it("accepts an empty successful JSON response", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null) });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, {
      status: 204,
      headers: { "content-type": "application/json" },
    })));

    await expect(api("/api/import-sources/source-1", { method: "DELETE" })).resolves.toBeUndefined();
  });
});
