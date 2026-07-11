import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssetImage } from "./asset-image";

afterEach(() => vi.restoreAllMocks());

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
});
