import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchShell } from "./shell";

const workspace = { id: "workspace-1", name: "Workspace One" };
const projects = [
  { id: "project-1", workspace_id: workspace.id, title: "Project One", state: "active" },
  { id: "project-2", workspace_id: "workspace-2", title: "Other Workspace Project", state: "active" },
];

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("workspace navigation", () => {
  it("renders the current workspace hierarchy and keeps nested project routes active", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/workspaces")) return response([workspace]);
      if (path.includes("/api/projects")) return response(projects);
      if (path.includes("/api/import-sources")) return response([]);
      if (path.includes("/api/activity")) return response([]);
      if (path.includes("/api/dashboard")) return response({});
      if (path.includes("/api/models")) return response([]);
      return response({});
    }));

    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    render(<WorkbenchShell section="models" id="" params={new URLSearchParams("project=project-1")}><main>Screen</main></WorkbenchShell>);
    await waitFor(() => expect(screen.getAllByText("Workspace One").length).toBeGreaterThan(0));
    expect(screen.getByText("Project One")).toBeInTheDocument();
    expect(screen.queryByText("Other Workspace Project")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Project One" })).toHaveAttribute("href", "#/project/project-1");
    expect(screen.getByRole("link", { name: "Project One" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("href", "#/models?project=project-1");
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "#/settings");
    expect(screen.getByText(/Local cache/)).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toHaveAttribute("data-layout", "scroll-region");
    expect(document.querySelector(".vela-inspector")).toHaveAttribute("data-layout", "scroll-region");
  });
});
