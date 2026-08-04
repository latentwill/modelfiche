import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { canonicalWorkspaceHref, legacyEntityReference, parseWorkspaceHash } from "./workspace-routing";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => {
  cleanup();
  location.hash = "";
  vi.unstubAllGlobals();
});

describe("canonical workspace routing", () => {
  it("parses canonical scope and strips legacy workspace query authority", () => {
    expect(parseWorkspaceHash("#/w/workspace-1/project/project-1?edit=1")).toEqual({
      workspaceId: "workspace-1",
      route: "project/project-1?edit=1",
      legacy: false,
    });
    expect(canonicalWorkspaceHref("#/run/run-1?workspace=stale&project=project-1", "workspace-1"))
      .toBe("#/w/workspace-1/run/run-1?project=project-1");
  });

  it.each([
    ["project", "project-1", "project"],
    ["dataset", "dataset-1", "dataset"],
    ["run", "run-1", "run"],
    ["training", "launch-1", "training-launch"],
  ])("redirects legacy %s links through server-resolved ownership", async (section, entityId, entityType) => {
    location.hash = `#/${section}/${entityId}`;
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => key === "titles.activeWorkspaceId" ? "workspace-stale" : null,
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/health") return response({ status: "ok" });
      if (path === `/api/workspace-resolutions/${entityType}/${entityId}`) {
        return response({ workspace_id: "workspace-owner", workspace_name: "Owner" });
      }
      return response([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => expect(location.hash).toBe(`#/w/workspace-owner/${section}/${entityId}`));
    expect(fetchMock).toHaveBeenCalledWith(`/api/workspace-resolutions/${entityType}/${entityId}`, expect.anything());
  });

  it("uses project ownership for legacy project-filtered catalog links", () => {
    expect(legacyEntityReference("datasets?project=project-7")).toEqual({ entityType: "project", entityId: "project-7" });
  });
});
