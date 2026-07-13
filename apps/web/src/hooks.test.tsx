import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

function ResourceProbe({ path }: { path: string }) {
  const resource = useResource<{ project: string }>(path);
  return <output>{resource.data?.project ?? "empty"}</output>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useResource", () => {
  it("does not retain a previous project's result after the route changes", async () => {
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

    modernTimes.resolve(new Response(JSON.stringify({ project: "modern-times" }), { headers: { "content-type": "application/json" } }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("empty"));

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

    expect(screen.getByRole("status")).toHaveTextContent("empty");
    vcribb.resolve(new Response(JSON.stringify({ project: "vcribb" }), { headers: { "content-type": "application/json" } }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("vcribb"));
  });
});
