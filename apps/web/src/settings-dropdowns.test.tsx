import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsScreen } from "./screens-core";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

const settings = {
  llm: { provider: "openai", model: "gpt-5-mini", base_url: null },
  fal: { max_parallel_jobs: 4 },
  generation: { prompt_prepend: "", prompt_append: "" },
  credentials: { llm_configured: true, fal_configured: false },
};

const source = {
  id: "source-1",
  name: "Archive",
  bucket: "archive",
  endpoint_url: null,
  region: "us-east-1",
  addressing_style: "path",
  credential_env_prefix: "S3",
  is_active: false,
  allowed_prefixes: [],
};

afterEach(() => vi.unstubAllGlobals());

describe("settings dropdowns", () => {
  it("renders shared dropdowns and saves OpenRouter without forcing its default endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PATCH" && path.includes("/api/operator-settings")) return response({ ...settings, llm: { provider: "openrouter", model: "openai/gpt-4o-mini", base_url: null } });
      if (path.includes("/api/system/doctor")) return response({ ok: true });
      if (path.includes("/api/operator-settings")) return response(settings);
      if (path.includes("/api/workspaces")) return response([{ id: "workspace-1", name: "Workspace" }]);
      if (path.includes("/api/profiles")) return response([{ id: "profile-1", display_name: "Operator", email: "operator@example.com", is_active: true }]);
      if (path.includes("/api/import-sources")) return response([source]);
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<SettingsScreen />);
    const provider = await screen.findByRole("button", { name: "Provider" });
    expect(document.querySelector("select")).toBeNull();
    fireEvent.click(provider);
    fireEvent.click(screen.getByRole("option", { name: "OpenRouter" }));

    expect(screen.getByText(/OpenRouter uses OPENROUTER_API_KEY/)).toBeInTheDocument();
    expect(screen.getByText(/standard https:\/\/openrouter\.ai\/api\/v1 endpoint/)).toBeInTheDocument();
    const form = screen.getByRole("button", { name: "Save provider settings" }).closest("form")!;
    expect(new FormData(form).get("llm_provider")).toBe("openrouter");
    expect(new FormData(form).get("llm_base_url")).toBe("");

    fireEvent.submit(form);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings", expect.objectContaining({ method: "PATCH" })));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({ llm: { provider: "openrouter", base_url: null } });
  });

  it("preserves S3 addressing and source-state defaults in form submission", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/system/doctor")) return response({ ok: true });
      if (path.includes("/api/operator-settings")) return response(settings);
      if (path.includes("/api/workspaces")) return response([{ id: "workspace-1", name: "Workspace" }]);
      if (path.includes("/api/profiles")) return response([{ id: "profile-1", display_name: "Operator", email: null, is_active: true }]);
      if (path.includes("/api/import-sources")) return response([source]);
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<SettingsScreen />);
    fireEvent.click(await screen.findByRole("link", { name: "Open Archive" }));
    const sourcePanel = screen.getByRole("heading", { name: "Edit Archive" }).closest("section")!;
    const panel = within(sourcePanel);
    const addressing = panel.getByRole("button", { name: "Addressing style" });
    const state = panel.getByRole("button", { name: "Source state" });
    expect(addressing).toHaveTextContent("Path");
    expect(state).toHaveTextContent("Inactive");
    const form = state.closest("form")!;
    expect(new FormData(form).get("addressing_style")).toBe("path");
    expect(new FormData(form).get("is_active")).toBe("false");
  });
});
