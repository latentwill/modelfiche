import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { canonicalWorkspaceHref, legacyEntityReference, parseWorkspaceHash } from "./workspace-routing";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  vi.stubGlobal("EventSource", class {
    addEventListener() {}
    close() {}
  });
});

afterEach(() => {
  cleanup();
  location.hash = "";
  vi.unstubAllGlobals();
});

describe("canonical workspace routing", () => {
  it("parses canonical scope and strips legacy workspace query authority", () => {
    expect(parseWorkspaceHash("#/w/workspace-1/project/project-1?edit=1")).toEqual({
      workspaceSlug: "workspace-1",
      route: "project/project-1?edit=1",
      legacy: false,
    });
    expect(canonicalWorkspaceHref("#/run/run-1?workspace=stale&project=project-1", "workspace-1"))
      .toBe("#/w/workspace-1/run/run-1?project=project-1");
  });

  it("switches same-section routes and reloads workspace-scoped navigation", async () => {
    const workspaces = [
      { id: "workspace-1", name: "Studio", slug: "studio" },
      { id: "workspace-2", name: "Personal", slug: "personal" },
    ];
    location.hash = "#/w/studio/dashboard";
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const workspaceSlug = new Headers(init?.headers).get("x-workspace-id");
      if (path === "/api/health") return response({ status: "ok" });
      if (path === "/api/workspaces") return response(workspaces);
      if (path.startsWith("/api/projects")) {
        return response(workspaceSlug === "personal"
          ? [{ id: "project-2", workspace_id: "workspace-2", title: "Personal Project", state: "active" }]
          : [{ id: "project-1", workspace_id: "workspace-1", title: "Studio Project", state: "active" }]);
      }
      if (path === "/api/dashboard") return response({});
      return response([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByRole("link", { name: "Studio Project" });

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    fireEvent.click(screen.getByRole("option", { name: "Personal" }));

    await waitFor(() => expect(location.hash).toBe("#/w/personal/dashboard"));
    await screen.findByRole("link", { name: "Personal Project" });
    expect(screen.queryByRole("link", { name: "Studio Project" })).not.toBeInTheDocument();
    expect(document.title).toBe("Dashboard - Personal - Modelfiche");
    expect(fetchMock.mock.calls.some(([, init]) => new Headers(init?.headers).get("x-workspace-id") === "personal")).toBe(true);
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
        return response({ workspace_id: "workspace-owner", workspace_name: "Owner", workspace_slug: "owner" });
      }
      return response([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => expect(location.hash).toBe(`#/w/owner/${section}/${entityId}`));
    expect(fetchMock).toHaveBeenCalledWith(`/api/workspace-resolutions/${entityType}/${entityId}`, expect.anything());
  });

  it("uses project ownership for legacy project-filtered catalog links", () => {
    expect(legacyEntityReference("datasets?project=project-7")).toEqual({ entityType: "project", entityId: "project-7" });
  });
});
