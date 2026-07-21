import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useResource } from "./hooks";

type DeferredResponse = {
  resolve: (response: Response) => void;
  promise: Promise<Response>;
};

function deferredResponse(): DeferredResponse {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>(complete => { resolve = complete; });
  return { resolve, promise };
}

function ResourceProbe({ path }: { path: string | null }) {
  const resource = useResource<{ project: string }>(path);
  const text = resource.loading ? "loading" : resource.error ? `error: ${resource.error}` : resource.data?.project ?? (path ? "empty" : "idle");
  return <output>{text}</output>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useResource", () => {
  it("keeps the current path loading when a stale previous result resolves", async () => {
    const modernTimes = deferredResponse();
    const vcribb = deferredResponse();
    const fetchMock = vi.fn()
      .mockReturnValueOnce(modernTimes.promise)
      .mockReturnValueOnce(vcribb.promise);
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<ResourceProbe path="/api/gallery?project_id=modern-times" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    view.rerender(<ResourceProbe path="/api/gallery?project_id=vcribb" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    await act(async () => { modernTimes.resolve(new Response(JSON.stringify({ project: "modern-times" }), { headers: { "content-type": "application/json" } })); });
    expect(screen.getByRole("status")).toHaveTextContent("loading");

    vcribb.resolve(new Response(JSON.stringify({ project: "vcribb" }), { headers: { "content-type": "application/json" } }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("vcribb"));
  });

  it("hides the previous project's data while the next project loads", async () => {
    const vcribb = deferredResponse();
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ project: "modern-times" }), { headers: { "content-type": "application/json" } }))
      .mockReturnValueOnce(vcribb.promise));

    const view = render(<ResourceProbe path="/api/gallery?project_id=modern-times" />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("modern-times"));
    view.rerender(<ResourceProbe path="/api/gallery?project_id=vcribb" />);

    expect(screen.getByRole("status")).toHaveTextContent("loading");
    vcribb.resolve(new Response(JSON.stringify({ project: "vcribb" }), { headers: { "content-type": "application/json" } }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("vcribb"));
  });

  it("ends loading and exposes the current path error", async () => {
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Archive unavailable")));

    render(<ResourceProbe path="/api/gallery?project_id=missing" />);

    expect(screen.getByRole("status")).toHaveTextContent("loading");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("error: Archive unavailable"));
  });

  it("clears a previous path error while the replacement path loads", async () => {
    const replacement = deferredResponse();
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", vi.fn()
      .mockRejectedValueOnce(new Error("First archive unavailable"))
      .mockReturnValueOnce(replacement.promise));

    const view = render(<ResourceProbe path="/api/gallery?project_id=missing" />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("error: First archive unavailable"));
    view.rerender(<ResourceProbe path="/api/gallery?project_id=replacement" />);

    expect(screen.getByRole("status")).toHaveTextContent("loading");
    replacement.resolve(new Response(JSON.stringify({ project: "replacement" }), { headers: { "content-type": "application/json" } }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("replacement"));
  });

  it("becomes idle immediately when a pending path is cleared", async () => {
    const pending = deferredResponse();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<ResourceProbe path="/api/gallery?project_id=pending" />);
    expect(screen.getByRole("status")).toHaveTextContent("loading");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    view.rerender(<ResourceProbe path={null} />);

    expect(screen.getByRole("status")).toHaveTextContent("idle");
    await act(async () => { pending.resolve(new Response(JSON.stringify({ project: "stale" }), { headers: { "content-type": "application/json" } })); });
    expect(screen.getByRole("status")).toHaveTextContent("idle");
  });

  it("keeps a null path idle without issuing a request", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", fetchMock);

    render(<ResourceProbe path={null} />);

    expect(screen.getByRole("status")).toHaveTextContent("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });

});
