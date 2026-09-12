import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssetImage } from "./asset-image";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("AssetImage", () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => null } });
  it("requests a server-owned descriptor before assigning the delivery URL", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      kind: "descriptor",
      descriptor: { delivery_url: "/api/asset-deliveries/token", asset_revision_id: "asset-1", variant: { kind: "thumbnail" } },
    }), { status: 200, headers: { "content-type": "application/json" } }));

    const view = render(<AssetImage assetRevisionId="asset-1" alt="Portrait" />);
    await waitFor(() => expect(view.getByRole("img")).toHaveAttribute("src", "/api/asset-deliveries/token"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/assets/asset-1/delivery",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ asset_revision_id: "asset-1", variant: { kind: "thumbnail", max_pixels: 512 } }) }),
    );
  });

  it("uses the server-provided alternate when a transient local image copy is missing", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        kind: "failure",
        failure: { redacted_message: "The preferred image copy is unavailable.", action: { kind: "consume_alternate_once", alternate_token: "alternate-token" } },
      }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        kind: "descriptor",
        descriptor: { delivery_url: "/api/asset-deliveries/remote-token", asset_revision_id: "alternate-asset", variant: { kind: "thumbnail" } },
      }), { status: 200, headers: { "content-type": "application/json" } }));

    const view = render(<AssetImage assetRevisionId="alternate-asset" alt="Recovered portrait" />);
    await waitFor(() => expect(view.getByRole("img")).toHaveAttribute("src", "/api/asset-deliveries/remote-token"));
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/assets/alternate-asset/delivery/alternate",
      expect.objectContaining({ method: "POST", body: JSON.stringify({
        asset_revision_id: "alternate-asset",
        variant: { kind: "thumbnail", max_pixels: 512 },
        alternate_token: "alternate-token",
        diagnostic_token: "alternate-token",
      }) }),
    );
  });

  it("defers delivery descriptor work for offscreen lazy images", async () => {
    let callback: IntersectionObserverCallback | undefined;
    const observe = vi.fn();
    const disconnect = vi.fn();
    Object.defineProperty(globalThis, "IntersectionObserver", { configurable: true, value: class {
      constructor(next: IntersectionObserverCallback) { callback = next; }
      observe = observe;
      disconnect = disconnect;
    } });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/lazy" } }), { status: 200, headers: { "content-type": "application/json" } }));

    const view = render(<AssetImage assetRevisionId="lazy-asset" loading="lazy" alt="Lazy portrait" />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(observe).toHaveBeenCalledWith(view.getByRole("img"));

    callback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
    await waitFor(() => expect(view.getByRole("img")).toHaveAttribute("src", "/delivery/lazy"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("refreshes an expired delivery descriptor after an image load failure", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/expired" } }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/fresh" } }), { status: 200, headers: { "content-type": "application/json" } }));
    const view = render(<AssetImage assetRevisionId="retry-asset" alt="Retry portrait" />);
    const image = await view.findByRole("img");
    await waitFor(() => expect(image).toHaveAttribute("src", "/delivery/expired"));
    fireEvent.error(image);
    await waitFor(() => expect(image).toHaveAttribute("src", "/delivery/fresh"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("uses an inspection mark while loading and a distinct neutral placeholder when unavailable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ kind: "failure", failure: { redacted_message: "Not hydrated", action: { kind: "hydrate" } } }), { status: 200, headers: { "content-type": "application/json" } }));
    const view = render(<AssetImage assetRevisionId="missing-asset" alt="Unavailable portrait" />);
    const image = view.getByRole("img");
    const loadingSrc = image.getAttribute("src");
    expect(image).toHaveAttribute("data-delivery-state", "loading");
    expect(loadingSrc).toContain("data:image/svg+xml");
    await waitFor(() => expect(image).toHaveAttribute("data-delivery-state", "unavailable"));
    expect(image.getAttribute("src")).toContain("data:image/svg+xml");
    expect(image.getAttribute("src")).not.toBe(loadingSrc);
  });
});

describe("image retry isolation", () => {
  it("retries an unavailable image without following its enclosing link", async () => {
    const navigate = vi.fn();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "failure", failure: { redacted_message: "Image unavailable", action: { kind: "stop" } } }), { headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/recovered" } }), { headers: { "content-type": "application/json" } }));
    const view = render(<a href="#/image/retry-isolation" onClick={navigate}><AssetImage assetRevisionId="retry-isolation" alt="Retry study" /></a>);
    fireEvent.click(await view.findByRole("button", { name: "Retry Retry study" }));
    expect(navigate).not.toHaveBeenCalled();
    await waitFor(() => expect(view.getByRole("img")).toHaveAttribute("src", "/delivery/recovered"));
  });
  it("ignores a pending retry after changing the image", async () => {
    let completeRetry!: (value: Response) => void;
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "failure", failure: { redacted_message: "Image unavailable", action: { kind: "stop" } } }), { headers: { "content-type": "application/json" } }))
      .mockImplementationOnce(() => new Promise(resolve => { completeRetry = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/current" } }), { headers: { "content-type": "application/json" } }));
    const view = render(<AssetImage assetRevisionId="retry-old" alt="Study" />);
    fireEvent.click(await view.findByRole("button", { name: "Retry Study" }));
    view.rerender(<AssetImage assetRevisionId="retry-current" alt="Study" />);
    await waitFor(() => expect(view.getByRole("img")).toHaveAttribute("src", "/delivery/current"));
    completeRetry(new Response(JSON.stringify({ kind: "descriptor", descriptor: { delivery_url: "/delivery/stale" } }), { headers: { "content-type": "application/json" } }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(view.getByRole("img")).toHaveAttribute("src", "/delivery/current");
  });
});
