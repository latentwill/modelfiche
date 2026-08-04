import { afterEach, describe, expect, it, vi } from "vitest";
import { api, establishRemoteSession } from "./api";

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
  it("derives request workspace from the route instead of stale storage", async () => {
    location.hash = "#/w/workspace-route/projects";
    vi.stubGlobal("localStorage", { getItem: vi.fn((key: string) => key === "titles.activeWorkspaceId" ? "workspace-stale" : null) });
    const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    await api("/api/projects");

    const headers = new Headers(fetch.mock.calls[0][1].headers);
    expect(headers.get("x-workspace-id")).toBe("workspace-route");
  });
  it("exchanges remote credentials for an HTTP-only session", async () => {
    vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null) });
    const fetch = vi.fn().mockResolvedValue(new Response('{"authenticated":true}', {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetch);

    await establishRemoteSession(" client-id ", "client-secret");

    expect(fetch).toHaveBeenCalledOnce();
    const headers = new Headers(fetch.mock.calls[0][1].headers);
    expect(headers.get("cf-access-client-id")).toBe("client-id");
    expect(headers.get("cf-access-client-secret")).toBe("client-secret");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });

});
