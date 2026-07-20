import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("settings dropdowns", () => {
  it("renders shared dropdowns and saves OpenRouter without forcing its default endpoint", async () => {
    let savedSettings = settings;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PATCH" && path.includes("/api/operator-settings")) {
        savedSettings = { ...settings, llm: { provider: "openrouter", model: "openai/gpt-4o-mini", base_url: null } };
        return response(savedSettings);
      }
      if (path.includes("/api/operator-settings/llm-key")) return response({ configured: true, source: "saved" });
      if (init?.method === "POST" && path.includes("/api/operator-settings/test/llm")) return response({ ok: true, mode: "network_validated", model: "openai/gpt-4o-mini", credential_source: "saved", catalog_size: 2, network_called: true });
      if (path.includes("/api/operator-settings/llm-models")) {
        const openrouter = path.includes("provider=openrouter");
        return response({
          provider: openrouter ? "openrouter" : "openai",
          models: openrouter
            ? [{ id: "openai/gpt-4o-mini", label: "GPT-4o Mini", description: "Vision", supports_images: true }, { id: "anthropic/claude-sonnet", label: "Claude Sonnet", description: "Vision", supports_images: true }]
            : [{ id: "gpt-5-mini", label: "GPT-5 Mini", description: "", supports_images: null }],
          credential: { configured: true, source: openrouter ? "saved" : "OPENAI_API_KEY" },
        });
      }
      if (path.includes("/api/system/doctor")) return response({ ok: true });
      if (path.includes("/api/operator-settings")) return response(savedSettings);
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
    fireEvent.click(await screen.findByRole("button", { name: "Load provider models" }));

    expect(screen.getByText(/OpenRouter uses OPENROUTER_API_KEY/)).toBeInTheDocument();
    const filter = await screen.findByRole("searchbox", { name: "Filter models" });
    fireEvent.change(filter, { target: { value: "gpt-4o" } });
    const model = await screen.findByRole("button", { name: "Model" });
    fireEvent.click(model);
    expect(screen.queryByRole("option", { name: /Claude Sonnet/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /GPT-4o Mini/ }));
    const llmPanel = screen.getByRole("heading", { name: "LLM" }).closest("section")!;
    expect(within(llmPanel).getByText(/OpenRouter credential · saved/)).toBeInTheDocument();
    fireEvent.click(within(llmPanel).getByRole("button", { name: "Validate LLM" }));
    expect(await within(llmPanel).findByText("OpenRouter validated")).toBeInTheDocument();
    expect(within(llmPanel).getByText(/credential source: saved/)).toBeInTheDocument();
    const key = within(llmPanel).getByPlaceholderText("OpenRouter credential configured — enter a replacement");
    fireEvent.change(key, { target: { value: "replacement-key" } });
    fireEvent.click(within(llmPanel).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings/llm-key", expect.objectContaining({ method: "PUT" })));
    expect(new FormData(screen.getByRole("button", { name: "Save LLM configuration" }).closest("form")!).get("llm_provider")).toBe("openrouter");
    expect(new FormData(screen.getByRole("button", { name: "Save LLM configuration" }).closest("form")!).get("llm_model")).toBe("openai/gpt-4o-mini");
    const form = screen.getByRole("button", { name: "Save LLM configuration" }).closest("form")!;
    expect(new FormData(form).get("llm_provider")).toBe("openrouter");
    expect(new FormData(form).get("llm_base_url")).toBe("");
    expect(new FormData(form).get("llm_model")).toBe("openai/gpt-4o-mini");

    fireEvent.click(screen.getByRole("button", { name: "Save LLM configuration" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings", expect.objectContaining({ method: "PATCH" })));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({ llm: { provider: "openrouter", base_url: null } });
    expect(await screen.findByText("Saved LLM configuration:", { exact: false })).toHaveTextContent("Saved LLM configuration: openrouter / openai/gpt-4o-mini");
    await waitFor(() => expect(within(screen.getByRole("button", { name: "Save all provider settings" }).closest("form")!).getByRole("button", { name: "Model" })).toHaveTextContent("GPT-4o Mini"));
  });

  it("persists an alternate model within the same OpenRouter provider", async () => {
    let savedSettings = { ...settings, llm: { provider: "openrouter", model: "openai/gpt-4.1-mini", base_url: null } };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PATCH" && path.includes("/api/operator-settings")) {
        const body = JSON.parse(String(init.body));
        savedSettings = { ...savedSettings, llm: body.llm };
        return response(savedSettings);
      }
      if (path.includes("/api/operator-settings/llm-models")) return response({
        provider: "openrouter",
        models: [
          { id: "openai/gpt-4.1-mini", label: "GPT-4.1 Mini", description: "Vision", supports_images: true },
          { id: "openai/gpt-5.6-luna", label: "GPT-5.6 Luna", description: "Vision", supports_images: true },
        ],
        credential: { configured: true, source: "saved" },
      });
      if (path.includes("/api/system/doctor")) return response({ ok: true });
      if (path.includes("/api/operator-settings")) return response(savedSettings);
      if (path.includes("/api/workspaces")) return response([{ id: "workspace-1", name: "Workspace" }]);
      if (path.includes("/api/profiles")) return response([{ id: "profile-1", display_name: "Operator", email: null, is_active: true }]);
      if (path.includes("/api/import-sources")) return response([]);
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<SettingsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Load provider models" }));
    const filter = await screen.findByRole("searchbox", { name: "Filter models" });
    fireEvent.change(filter, { target: { value: "luna" } });
    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("option", { name: /GPT-5.6 Luna/ }));
    expect(screen.getByText("Unsaved LLM selection")).toBeInTheDocument();
    expect(screen.getByText("OpenRouter / openai/gpt-5.6-luna")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save LLM configuration" }));

    await waitFor(() => expect(screen.getByText("Saved LLM configuration:", { exact: false })).toHaveTextContent("Saved LLM configuration: openrouter / openai/gpt-5.6-luna"));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({ llm: { provider: "openrouter", model: "openai/gpt-5.6-luna", base_url: null } });
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
    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
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
  it("saves and clears the write-only LLM key without rendering the secret", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/api/operator-settings/llm-key")) return response({});
      if (path.includes("/api/system/doctor")) return response({ ok: true });
      if (path.includes("/api/operator-settings")) return response(settings);
      if (path.includes("/api/workspaces")) return response([{ id: "workspace-1", name: "Workspace" }]);
      if (path.includes("/api/profiles")) return response([{ id: "profile-1", display_name: "Operator", email: null, is_active: true }]);
      if (path.includes("/api/import-sources")) return response([]);
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<SettingsScreen />);
    const llmPanel = (await screen.findByRole("heading", { name: "LLM" })).closest("section")!;
    const key = within(llmPanel).getByPlaceholderText("OpenAI credential configured — enter a replacement");
    fireEvent.change(key, { target: { value: "secret-llm-key" } });
    fireEvent.click(within(llmPanel).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings/llm-key", expect.objectContaining({ method: "PUT" })));
    expect(JSON.parse(String(fetchMock.mock.calls.find(([path, init]) => String(path).includes("/api/operator-settings/llm-key") && init?.method === "PUT")?.[1]?.body))).toEqual({ key: "secret-llm-key" });
    expect(key).toHaveValue("");
    expect(screen.queryByText("secret-llm-key")).not.toBeInTheDocument();

    fireEvent.click(within(llmPanel).getByRole("button", { name: "Clear saved key" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings/llm-key", expect.objectContaining({ method: "DELETE" })));
  });
