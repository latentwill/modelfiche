import { useRef, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, apiUnscoped, ApiError, setActiveWorkspaceId } from "./api";
import { App } from "./App";
import { GalleryScreen } from "./screens-assets";
import { WorkbenchShell } from "./shell";
import { Dropdown, Field, Form, useFocusWorkspace } from "./ui";
import { parseWorkspaceHash } from "./workspace-routing";
import { RouteBoundary } from "./route-boundary";

vi.mock("./asset-image", () => ({ AssetImage: ({ alt }: { alt: string }) => <img alt={alt} /> }));
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const workspace = { id: "w1", slug: "studio", name: "Studio" };
const storage = new Map<string, string>();
beforeEach(() => {
  storage.clear();
  location.hash = "#/w/studio/dashboard";
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value), removeItem: (key: string) => storage.delete(key) });
  vi.stubGlobal("scrollTo", vi.fn());
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("request and preference recovery", () => {
  it.each([api, apiUnscoped])("explains structured validation errors", async request => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: [{ loc: ["body", "name"], msg: "Field required" }] }, 422)));
    await expect(request("/api/projects")).rejects.toThrow("name: Field required");
  });
  it("preserves status and explains workspace conflicts", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: { code: "workspace_entity_mismatch", message: "Dataset belongs to another workspace" } }, 409)));
    await expect(api("/api/datasets/d1")).rejects.toMatchObject({ status: 409, message: "Dataset belongs to another workspace" });
  });
  it("retains HTTP status when a proxy sends broken JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{broken", { status: 502, headers: { "content-type": "application/json" } })));
    await expect(api("/api/projects")).rejects.toBeInstanceOf(ApiError);
  });
  it("does not require browser storage to send a scoped request", async () => {
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); } });
    const fetch = vi.fn().mockResolvedValue(response([])); vi.stubGlobal("fetch", fetch);
    expect(() => setActiveWorkspaceId("w1")).not.toThrow();
    await expect(api("/api/projects")).resolves.toEqual([]);
    expect(new Headers(fetch.mock.calls[0][1].headers).get("x-workspace-id")).toBe("studio");
  });
  it("keeps the chosen profile until the workspace actually changes", () => {
    storage.set("titles.activeWorkspaceId", "w1"); storage.set("titles.activeProfileId", "p1");
    setActiveWorkspaceId("w1"); expect(storage.get("titles.activeProfileId")).toBe("p1");
    setActiveWorkspaceId("w2"); expect(storage.has("titles.activeProfileId")).toBe(false);
  });
  it("can parse a malformed copied workspace link", () => {
    expect(parseWorkspaceHash("#/w/%E0%A4%A/dashboard").route).toBe("dashboard");
  });
});

describe("forms and nested focus", () => {
  it("submits once while pending, preserves inputs on error, and permits retry", async () => {
    let fail!: (reason: Error) => void;
    const submit = vi.fn().mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; })).mockResolvedValueOnce(undefined);
    render(<Form onSubmit={submit}><Field label="Name"><input name="name" defaultValue="Portraits" /></Field></Form>);
    const form = screen.getByRole("button", { name: "Save" }).closest("form")!;
    fireEvent.submit(form); fireEvent.submit(form);
    expect(submit).toHaveBeenCalledTimes(1);
    await act(async () => fail(new Error("Connection interrupted")));
    expect(screen.getByRole("alert")).toHaveTextContent("Connection interrupted");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Portraits");
    fireEvent.submit(form); await screen.findByRole("button", { name: "Saved" });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Landscapes" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
  it("marks changed dropdown values unsaved", async () => {
    render(<Form onSubmit={vi.fn()}><Dropdown name="kind" aria-label="Kind" defaultValue="a" options={[{ value: "a", label: "A" }, { value: "b", label: "B" }]} /></Form>);
    fireEvent.click(screen.getByRole("button", { name: "Save" })); await screen.findByRole("button", { name: "Saved" });
    fireEvent.click(screen.getByRole("button", { name: "Kind" })); fireEvent.click(screen.getByRole("option", { name: "B" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
  it("does not mark edits made during a pending save as saved", async () => {
    let complete!: () => void;
    render(<Form onSubmit={() => new Promise<void>(resolve => { complete = resolve; })}><input aria-label="Name" defaultValue="Original" /></Form>);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Newer edit" } });
    await act(async () => complete());
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Saved" })).not.toBeInTheDocument();
  });
  it("closes a dropdown before dismissing its containing dialog and protects outer background", async () => {
    function Harness() {
      const [open, setOpen] = useState(true); const ref = useRef<HTMLDivElement>(null);
      useFocusWorkspace({ active: open, containerRef: ref, onDismiss: () => setOpen(false) });
      return <><aside data-testid="outer">Background</aside><main>{open && <div ref={ref} role="dialog"><Dropdown aria-label="Choice" options={[{ value: "a", label: "A" }]} /></div>}</main></>;
    }
    render(<Harness />);
    expect(screen.getByTestId("outer")).toHaveAttribute("aria-hidden", "true");
    const choice = screen.getByRole("button", { name: "Choice" });
    fireEvent.click(choice); fireEvent.keyDown(choice, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument(); expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(choice, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(screen.getByTestId("outer")).not.toHaveAttribute("aria-hidden");
  });
});

function shellFetch(input: RequestInfo | URL) {
  const path = String(input);
  return Promise.resolve(response(path === "/api/workspaces" ? [workspace] : path.startsWith("/api/search") ? { results: [{ id: "project-a", type: "project", title: "Portraits" }] } : []));
}
describe("workbench navigation", () => {
  it("offers recovery from a failed view and clears the failure on navigation", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function BrokenView() { throw new Error("Malformed view data"); return null; }
    const view = render(<RouteBoundary route="dataset/a"><BrokenView /></RouteBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("Modelfiche could not display this view");
    expect(screen.getByRole("button", { name: "Reload view" })).toBeEnabled();
    expect(screen.getByRole("link", { name: "Return to dashboard" })).toHaveAttribute("href", "#/dashboard");
    view.rerender(<RouteBoundary route="dashboard"><h1>Dashboard</h1></RouteBoundary>);
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
  });
  it("supports menu keys, focus return, and outside dismissal", async () => {
    vi.stubGlobal("fetch", vi.fn(shellFetch));
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main><h1>Dashboard</h1></main></WorkbenchShell>);
    await screen.findByText("Studio");
    const button = screen.getByRole("button", { name: "Create or import" });
    fireEvent.click(button); const first = screen.getByRole("menuitem", { name: "Project" });
    expect(first).toHaveFocus(); fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Image" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" }); expect(button).toHaveFocus();
    fireEvent.click(button); fireEvent.pointerDown(document.body); expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
  it("skips to content without changing the route", async () => {
    vi.stubGlobal("fetch", vi.fn(shellFetch));
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main>Dashboard</main></WorkbenchShell>);
    fireEvent.click(screen.getByRole("link", { name: "Skip to main content" }));
    expect(document.getElementById("vela-route-main")).toHaveFocus(); expect(location.hash).toBe("#/w/studio/dashboard");
  });
  it("dismisses search results when focus leaves search", async () => {
    vi.stubGlobal("fetch", vi.fn(shellFetch));
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main>Dashboard</main></WorkbenchShell>);
    const search = screen.getByRole("combobox", { name: "Search workspace" });
    fireEvent.focus(search); fireEvent.change(search, { target: { value: "por" } });
    await screen.findByRole("option", { name: /Portraits/ });
    fireEvent.blur(search, { relatedTarget: screen.getByRole("button", { name: "Create or import" }) });
    expect(screen.queryByRole("listbox", { name: "Workspace search results" })).not.toBeInTheDocument();
  });
  it("does not navigate to old search results while a new query is pending", async () => {
    vi.stubGlobal("fetch", vi.fn(shellFetch));
    render(<WorkbenchShell section="dashboard" id="" params={new URLSearchParams()}><main>Dashboard</main></WorkbenchShell>);
    const search = screen.getByRole("combobox", { name: "Search workspace" });
    fireEvent.focus(search); fireEvent.change(search, { target: { value: "por" } });
    await screen.findByRole("option", { name: /Portraits/ });
    fireEvent.keyDown(search, { key: "ArrowUp" });
    expect(search).toHaveAttribute("aria-activedescendant", "global-search-option-0");
    fireEvent.change(search, { target: { value: "new query" } });
    fireEvent.keyDown(search, { key: "ArrowDown" }); fireEvent.keyDown(search, { key: "Enter" });
    expect(location.hash).toBe("#/w/studio/dashboard");
    expect(search).not.toHaveAttribute("aria-activedescendant");
  });
  it("discards the previous dataset's editor state on an object change", async () => {
    location.hash = "#/w/studio/dataset/a";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/health") return response({ ok: true });
      if (path === "/api/workspaces") return response([workspace]);
      const match = path.match(/^\/api\/datasets\/([ab])$/);
      if (match) return response({ id: match[1], name: `Dataset ${match[1]}`, current_version_id: `v-${match[1]}` });
      if (path.endsWith("/drafts/active")) return response(null);
      if (path.endsWith("/versions")) return response([]);
      return response([]);
    }));
    render(<App />);
    await screen.findByRole("heading", { name: "Dataset a", level: 1 });
    // Notes live in the inspector and used to persist across dataset routes.
    const note = screen.getByRole("textbox", { name: "Markdown note" });
    fireEvent.change(note, { target: { value: "Only for dataset a" } });
    await act(async () => { location.hash = "#/w/studio/dataset/b"; window.dispatchEvent(new HashChangeEvent("hashchange")); });
    await screen.findByRole("heading", { name: "Dataset b", level: 1 });
    expect(screen.getByRole("textbox", { name: "Markdown note" })).toHaveValue("");
  });
});

describe("gallery state", () => {
  it("rejects invalid pagination and makes filter changes shareable", async () => {
    location.hash = "#/w/studio/gallery?page=-4&pageSize=1000000";
    const fetch = vi.fn(async (input: RequestInfo | URL) => response(String(input).startsWith("/api/gallery") ? { items: [], total: 0 } : []));
    vi.stubGlobal("fetch", fetch);
    render(<GalleryScreen params={new URLSearchParams("page=-4&pageSize=1000000")} />);
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => String(path).includes("limit=50&offset=0"))).toBe(true));
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "portrait" } });
    expect(location.hash).toContain("q=portrait"); expect(location.hash).toContain("page=1");
    await screen.findByText("No images match these filters");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(location.hash).not.toContain("q=");
  });
  it("keeps an explicit page when gallery filters change through a link", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => response(String(input).startsWith("/api/gallery") ? { items: [], total: String(input).includes("q=portrait") ? 500 : 6 } : [])));
    const view = render(<GalleryScreen params={new URLSearchParams("page=1")} />);
    await screen.findByText("Page 1 of 1");
    view.rerender(<GalleryScreen params={new URLSearchParams("page=3&q=portrait")} />);
    await screen.findByText("Page 3 of 10");
    expect(screen.getByRole("textbox", { name: "Search" })).toHaveValue("portrait");
  });
});
