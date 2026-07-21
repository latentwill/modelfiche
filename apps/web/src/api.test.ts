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
  it("sends the selected workspace with every request", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn((key: string) => key === "titles.activeWorkspaceId" ? "workspace-personal" : null) });
    const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    await api("/api/projects");

    const headers = new Headers(fetch.mock.calls[0][1].headers);
    expect(headers.get("x-workspace-id")).toBe("workspace-personal");
  });

});
