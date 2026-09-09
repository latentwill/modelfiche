import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActiveTrainingPanel, orderActiveRuns } from "./active-training";
import { ProjectScreen } from "./screens-core";
import { DashboardScreen } from "./screens-wireframe";
import { WorkbenchShell } from "./shell";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

const runningRun = {
  id: "run-live",
  name: "Portrait live",
  project_id: "project-1",
  status: "running",
  current_step: 84,
  latest_loss: { name: "train/objective", step: 84, value_text: "0.184" },
  learning_rate: { name: "learning_rate", step: 84, value: 0.0001 },
  elapsed_seconds: 125,
  upload_status: { pending: 2, uploaded: 1 },
  last_event_at: "2026-07-13T10:00:00Z",
};

function installStorage() {
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("active training ordering", () => {
  it("includes only running work and excludes queued or terminal records", () => {
    expect(orderActiveRuns([
      { id: "pending-new", status: "pending", last_event_at: "2026-07-13T12:00:00Z" },
      { id: "completed", status: "completed", last_event_at: "2026-07-13T13:00:00Z" },
      { id: "running-old", status: "running", last_event_at: "2026-07-13T09:00:00Z" },
      { id: "running-new", status: "running", last_event_at: "2026-07-13T11:00:00Z" },
    ]).map(run => run.id)).toEqual(["running-new", "running-old"]);
  });

  it("uses one bounded running-status query and exposes status in the run link name", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs?status=running&limit=6") return response([runningRun]);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    installStorage();

    render(<ActiveTrainingPanel />);

    const list = await screen.findByLabelText("Active training runs");
    const links = within(list).getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAccessibleName("Open Portrait live, status running");
    expect(within(links[0]).getByLabelText("Status: running")).toBeInTheDocument();
    expect(links[0]).toHaveTextContent("0.0001");
    expect(links[0]).toHaveTextContent("Uploads: 2 pending · 1 uploaded");
    expect(links[0]).not.toHaveTextContent("[object Object]");
    expect(fetchMock).toHaveBeenCalledWith("/api/runs?status=running&limit=6", expect.any(Object));
    expect(fetchMock).not.toHaveBeenCalledWith("/api/runs?status=pending&limit=6", expect.any(Object));
  });

  it("hides the panel when a run was created but never started", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/runs?status=running&limit=6") return response([]);
      throw new Error(`Unexpected request: ${input}`);
    }));
    installStorage();

    render(<ActiveTrainingPanel />);

    await waitFor(() => expect(screen.queryByRole("heading", { name: "Active training" })).not.toBeInTheDocument());
  });
});

describe("active training surfaces", () => {
  it("keeps a running run above ordinary dashboard content when recent results omit it", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/projects?limit=100") return response([]);
      if (path === "/api/runs?limit=100") return response([{ id: "run-recent", name: "Yesterday", status: "completed" }]);
      if (path === "/api/runs?status=running&limit=6") return response([runningRun]);
      if (path === "/api/runs?status=pending&limit=6") return response([]);
      throw new Error(`Unexpected request: ${path}`);
    }));
    installStorage();

    render(<DashboardScreen />);

    const active = await screen.findByRole("link", { name: "Open Portrait live, status running" });
    const recentHeading = screen.getByRole("heading", { name: "Recent runs" });
    expect(active.compareDocumentPosition(recentHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("link", { name: /Yesterday/ })).toBeInTheDocument();
  });

  it("places project-scoped active training before the latest imported run", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/projects/project-1") return response({ id: "project-1", title: "Portraits", trigger_words: [] });
      if (path === "/api/runs?project_id=project-1&status=running&limit=6") return response([runningRun]);
      if (path === "/api/runs?project_id=project-1&status=pending&limit=6") return response([]);
      if (path === "/api/runs?project_id=project-1") return response([{ id: "run-old", name: "Imported run", status: "completed", created_at: "2026-07-12T00:00:00Z" }]);
      if (path.includes("/api/runs/run-old/")) return response(path.endsWith("/config") ? { normalized: {} } : []);
      if (path.includes("/api/datasets") || path.includes("/api/models") || path.includes("/api/eval-runs") || path.includes("/api/gallery")) return response([]);
      throw new Error(`Unexpected request: ${path}`);
    }));
    installStorage();

    render(<ProjectScreen id="project-1" />);

    const active = await screen.findByRole("link", { name: "Open Portrait live, status running" });
    const latestHeading = screen.getByRole("heading", { name: "Latest training run" });
    expect(active.compareDocumentPosition(latestHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("makes active runs navigable inside an expanded project sidebar", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/workspaces") return response([{ id: "workspace-1", name: "Workspace" }]);
      if (path === "/api/projects?limit=100") return response([{ id: "project-1", workspace_id: "workspace-1", title: "Portraits", state: "active" }]);
      if (path === "/api/runs?project_id=project-1&status=running&limit=4") return response([runningRun]);
      if (path === "/api/runs?project_id=project-1&status=pending&limit=4") return response([]);
      if (path.includes("/api/import-sources") || path.includes("/api/activity") || path.includes("/api/models") || path.includes("/api/datasets") || path.includes("/api/eval-runs")) return response([]);
      if (path === "/api/projects/project-1") return response({ id: "project-1", title: "Portraits" });
      return response({});
    }));
    installStorage();
    location.hash = "#/w/workspace-1/project/project-1";

    render(<WorkbenchShell section="project" id="project-1" params={new URLSearchParams()}><main>Project</main></WorkbenchShell>);

    const link = await screen.findByRole("link", { name: "Portrait live, status running" });
    expect(link).toHaveAttribute("href", "#/w/workspace-1/run/run-live?project=project-1");
    expect(link).toHaveTextContent("LIVE");
    await waitFor(() => expect(screen.getByRole("link", { name: "Active training" })).toHaveAttribute("href", "#/w/workspace-1/runs?project=project-1"));
  });
});
