import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReadinessScreen } from "./readiness";

function response(value: unknown) {
  return new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("first-run readiness", () => {
  it("shows five checks with an exact next action and runs safe connection tests", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/api/system/doctor")) return Promise.resolve(response({ database: { ok: true }, asset_store: { ok: true }, backup: { configured: false }, agent_cli: { configured: false, install_available: true, install_path: "/Users/test/.local/bin/mfiche" } }));
      if (path.includes("/api/operator-settings/test/fal") && init?.method === "POST") return Promise.resolve(response({ ok: true }));
      if (path.includes("/api/operator-settings")) return Promise.resolve(response({ credentials: { fal_configured: true } }));
      if (path.includes("/api/import-sources/source-1/test") && init?.method === "POST") return Promise.resolve(response({ ok: true }));
      if (path.includes("/api/import-sources")) return Promise.resolve(response([{ id: "source-1", name: "Training S3", is_active: true }]));
      if (path.includes("/api/training-setup")) return Promise.resolve(response({ ready: false, checks: [{ status: "blocked", fix: "Connect an active S3 source." }] }));
      return Promise.resolve(response({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() });

    render(<ReadinessScreen />);

    expect(await screen.findAllByRole("heading", { level: 2 })).toHaveLength(5);
    expect(await screen.findByText("Run mfiche backup create, then check again.")).toBeInTheDocument();
    expect(await screen.findByText("A write-only FAL credential is configured.")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("heading", { name: "Generation provider" }).closest("section")!).getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings/test/fal", expect.objectContaining({ method: "POST" })));
    const installButton = within(screen.getByRole("heading", { name: "Agent CLI" }).closest("section")!).getByRole("button", { name: "Install command" });
    await waitFor(() => expect(installButton).not.toBeDisabled());
    fireEvent.click(installButton);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/system/cli-install", expect.objectContaining({ method: "POST" })));
  });
});
