import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardScreen, TransfersScreen } from "./screens-wireframe";
import { WorkbenchShell } from "./shell";

vi.mock("./asset-image", () => ({
  AssetImage: ({ assetRevisionId, alt }: { assetRevisionId: string; alt: string }) => <img data-testid={`asset-image-${assetRevisionId}`} alt={alt} />,
}));

const workspace = { id: "workspace-1", name: "Workspace One" };
const personalWorkspace = { id: "workspace-2", name: "Personal" };
const projects = [
  { id: "project-1", workspace_id: workspace.id, title: "Project One", state: "active" },
  { id: "project-2", workspace_id: "workspace-2", title: "Other Workspace Project", state: "active" },
];

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("workspace navigation", () => {
  it("renders the current workspace hierarchy and keeps nested project routes active", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/workspaces")) return response([workspace, personalWorkspace]);
      if (path.includes("/api/projects")) return response(projects);
      if (path.includes("/api/import-sources")) return response([]);
      if (path.includes("/api/activity")) return response([]);
      if (path.includes("/api/dashboard")) return response({});
      if (path.includes("/api/models")) return response([]);
      return response({});
    }));

    vi.stubGlobal("localStorage", { getItem: () => "workspace-stale", setItem: vi.fn(), removeItem: vi.fn() });
    location.hash = "#/w/workspace-1/models?project=project-1";
    render(<WorkbenchShell section="models" id="" params={new URLSearchParams("project=project-1")}><main>Screen</main></WorkbenchShell>);
    await waitFor(() => expect(screen.getByText("Project One")).toBeInTheDocument());
    expect(screen.queryByText("Other Workspace Project")).not.toBeInTheDocument();
    const workspaceSwitcher = screen.getByRole("button", { name: "Workspace" });
    expect(workspaceSwitcher).toHaveTextContent("Workspace One");
    fireEvent.click(workspaceSwitcher);
    expect(screen.getByRole("option", { name: "Workspace One" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Personal" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Project One" })).toHaveAttribute("href", "#/w/workspace-1/project/project-1");
    expect(screen.getByRole("link", { name: "Project One" })).toHaveAttribute("aria-current", "page");
    window.dispatchEvent(new CustomEvent("titles:project-updated", { detail: { ...projects[0], title: "Renamed Project" } }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Renamed Project" })).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: "Project One" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("href", "#/w/workspace-1/models?project=project-1");
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "#/w/workspace-1/settings");
    expect(screen.getByRole("link", { name: "Docs" })).toHaveAttribute("href", "#/w/workspace-1/docs");
    expect(screen.queryByRole("link", { name: "Setup" })).not.toBeInTheDocument();
    expect(screen.getByText(/Local cache/)).toBeInTheDocument();

    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toHaveAttribute("data-layout", "scroll-region");
    expect(document.querySelector(".vela-inspector")).toHaveAttribute("data-layout", "scroll-region");
    fireEvent.click(screen.getByRole("button", { name: "Create or import" }));
    const createItems = screen.getAllByRole("menuitem").map(item => item.textContent);
    expect(createItems).not.toContain("Eval");
    expect(createItems.indexOf("Image")).toBeLessThan(createItems.indexOf("Grid"));
    expect(screen.getByRole("menuitem", { name: "S3 import" })).toHaveAttribute("href", "#/w/workspace-1/import?source=s3&project=project-1");
    expect(screen.getByRole("menuitem", { name: "Local folder import" })).toHaveAttribute("href", "#/w/workspace-1/import?source=local&project=project-1");
    expect(screen.getByRole("menuitem", { name: "Queue import (Transfers)" })).toHaveAttribute("href", "#/w/workspace-1/transfers?mode=import&project=project-1");
    expect(screen.queryByRole("menuitem", { name: "Import" })).not.toBeInTheDocument();
  });
  it("links sidebar overflow to the complete model and grid catalogs", async () => {
    const models = Array.from({ length: 6 }, (_, index) => ({ id: `model-${index}`, project_id: "project-1", name: `Model ${index + 1}` }));
    const grids = Array.from({ length: 6 }, (_, index) => ({ id: `grid-${index}`, project_id: "project-1", name: `Grid ${index + 1}` }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/workspaces")) return response([workspace]);
      if (path.includes("/api/projects/project-1/grids")) return response(grids);
      if (path.includes("/api/projects")) return response([projects[0]]);
      if (path.includes("/api/import-sources")) return response([]);
      if (path.includes("/api/models")) return response(models);
      if (path.includes("/api/datasets")) return response([]);
      if (path.includes("/api/activity")) return response([]);
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    location.hash = "#/w/workspace-1/models?project=project-1";

    render(<WorkbenchShell section="models" id="" params={new URLSearchParams("project=project-1")}><main>Screen</main></WorkbenchShell>);

    const allModels = await screen.findByRole("link", { name: "View all 6 models" });
    expect(allModels).toHaveAttribute("href", "#/w/workspace-1/models?project=project-1");
    expect(screen.queryByRole("link", { name: "Model 6" })).not.toBeInTheDocument();
    const allGrids = await screen.findByRole("link", { name: "View all 6 grids" });
    expect(allGrids).toHaveAttribute("href", "#/w/workspace-1/grids?project=project-1");
    expect(screen.queryByRole("link", { name: "Grid 6" })).not.toBeInTheDocument();
  });
  it("links the transfer import shortcut to the S3 browser with project context", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/jobs")) return response([]);
      if (path.includes("/api/transfers")) return response([]);
      if (path.includes("/api/import-sources")) return response([]);
      if (path.includes("/api/projects")) return response([]);
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    render(<TransfersScreen mode="import" params={new URLSearchParams("project=project-1")} />);
    await waitFor(() => expect(screen.getByRole("link", { name: "Open full S3 browser" })).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Open full S3 browser" })).toHaveAttribute("href", "#/import?source=s3&project=project-1");
  });
  it("renders linked FAL/eval previews and leaves fallback activity unlinked", async () => {
    const activity = [
      { id: "event-eval", action: "fal.admission_admitted", summary: "Generation admitted", object_label: "Eval run 42", object_href: "#/eval/eval-42", preview_asset_id: "revision-eval-42", profile_name: "Operator", created_at: "2026-07-13T10:00:00Z" },
      { id: "event-deleted", action: "asset.deleted", summary: "Asset Deleted", object_label: "deleted.png", object_href: "#/image/deleted", preview_asset_id: "revision-deleted", profile_name: "Operator", created_at: "2026-07-13T09:30:00Z" },
      { id: "event-other", action: "workspace.updated", summary: "Workspace metadata updated", object_label: "Workspace One", object_href: null, preview_asset_id: null, profile_name: null, created_at: "2026-07-13T09:00:00Z" },
    ];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      switch (new URL(raw, "http://test.local").pathname) {
        case "/api/workspaces": return response([workspace]);
        case "/api/projects": return response(projects);
        case "/api/import-sources": return response([]);
        case "/api/activity": return response(activity);
        case "/api/dashboard": return response({});
        default: return response({});
      }
    }));

    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    location.hash = "#/w/workspace-1/dashboard";
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main>Screen</main></WorkbenchShell>);
    await waitFor(() => expect(screen.getByRole("link", { name: "Eval run 42" })).toBeInTheDocument());
    const evalLink = screen.getByRole("link", { name: "Eval run 42" });
    expect(evalLink).toHaveAttribute("href", "#/w/workspace-1/eval/eval-42");
    expect(evalLink).not.toContainElement(screen.getByTestId("asset-image-revision-eval-42"));
    const galleryLink = screen.getByRole("link", { name: "Open Eval run 42 in gallery" });
    expect(galleryLink).toHaveAttribute("href", expect.stringContaining("#/w/workspace-1/gallery?"));
    expect(galleryLink).toHaveAttribute("href", expect.stringContaining("eval=eval-42"));
    expect(galleryLink).toHaveAttribute("href", expect.stringContaining("asset=revision-eval-42"));
    expect(galleryLink).toContainElement(screen.getByTestId("asset-image-revision-eval-42"));
    expect(screen.getByText("by Operator")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Generation admitted" })).toBeInTheDocument();
    expect(document.querySelector('time[datetime="2026-07-13T10:00:00Z"]')).toBeInTheDocument();
    expect(screen.queryByText("fal.admission_admitted")).not.toBeInTheDocument();
    expect(screen.queryByText("Asset Deleted")).not.toBeInTheDocument();
    expect(screen.getByText(/Workspace metadata updated/).closest("a")).toBeNull();
    expect(screen.getByText("Workspace · Updated")).toBeInTheDocument();
  });

  it("renders a recent-run preview without changing the run route", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/projects")) return response([]);
      if (path.includes("/api/runs")) return response([{ id: "run-1", name: "Recent Run", status: "completed", base_model: "base-v1", preview_asset_id: "revision-run-1", updated_at: "2026-07-13T10:00:00Z" }]);
      return response({});
    }));

    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DashboardScreen />);
    await waitFor(() => expect(screen.getByText("Recent Run")).toBeInTheDocument());
    expect(screen.getByTestId("asset-image-revision-run-1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Recent Run/ })).toHaveAttribute("href", "#/run/run-1");
  });

  it("falls back from eval previews to samples and then dataset images", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/projects?limit=100") return response([{ id: "project-1", title: "Dataset only" }]);
      if (path.includes("category=eval_output")) return response([]);
      if (path.includes("category=sample")) return response([]);
      if (path.includes("category=dataset_image")) return response([{ id: "dataset-image-1" }]);
      return response([]);
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DashboardScreen />);

    const preview = await screen.findByLabelText("1 latest dataset images");
    expect(preview).toContainElement(screen.getByTestId("asset-image-dataset-image-1"));
  });

  it("prefers samples over dataset images when no eval output exists", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/projects?limit=100") return response([{ id: "project-1", title: "Sample project" }]);
      if (path.includes("category=eval_output")) return response([]);
      if (path.includes("category=sample")) return response([{ id: "sample-image-1" }]);
      if (path.includes("category=dataset_image")) return response([{ id: "dataset-image-1" }]);
      return response([]);
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DashboardScreen />);

    const preview = await screen.findByLabelText("1 latest training samples");
    expect(preview).toContainElement(screen.getByTestId("asset-image-sample-image-1"));
    expect(screen.queryByTestId("asset-image-dataset-image-1")).not.toBeInTheDocument();
  });

  it("contains mobile navigation focus and restores its opener on Escape", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/workspaces")) return response([workspace]);
      if (path.includes("/api/projects")) return response(projects);
      return response([]);
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main><h1>Dashboard</h1></main></WorkbenchShell>);
    const opener = screen.getAllByRole("button", { name: "Open navigation" }).at(-1)!;
    opener.focus();
    fireEvent.click(opener);
    const drawer = await screen.findByRole("dialog", { name: "Primary navigation" });
    await waitFor(() => expect(drawer).toContainElement(document.activeElement as HTMLElement));
    expect(drawer.parentElement?.querySelector(".vela-workspace")).toHaveAttribute("aria-hidden", "true");
    fireEvent.keyDown(drawer, { key: "Escape" });
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog", { name: "Primary navigation" })).not.toBeInTheDocument();
  });
  it("copies metadata from the global inspector", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", Object.create(navigator, { clipboard: { value: { writeText }, configurable: true } }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/api/projects/project-1")) return response(projects[0]);
      if (path.includes("/api/workspaces")) return response([workspace]);
      if (path.includes("/api/projects")) return response([projects[0]]);
      if (path.includes("/api/import-sources") || path.includes("/api/activity")) return response([]);
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<WorkbenchShell section="project" id="project-1" params={new URLSearchParams("project=project-1")}><main>Project</main></WorkbenchShell>);

    const inspector = document.querySelector(".vela-inspector");
    expect(inspector).toHaveClass("vela-copyable-metadata");
    fireEvent.click(await screen.findByRole("button", { name: "JSON" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(JSON.parse(String(writeText.mock.calls[0]?.[0]))).toMatchObject({ id: "project-1", title: "Project One" });
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});
