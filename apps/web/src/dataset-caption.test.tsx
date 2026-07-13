import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DatasetScreen } from "./screens-assets";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("dataset caption generation controls", () => {
  it("generates AI captions for the draft scope without publishing", async () => {
    const items = [
      { id: "item-1", filename: "portrait.png", caption: "old caption", included: true, asset_revision_id: "asset-1" },
      { id: "item-2", filename: "landscape.png", caption: "old landscape caption", included: true, asset_revision_id: "asset-2" },
    ];
    const draft = { id: "draft-1", items };
    let releaseCaption: () => void = () => undefined;
    const captionGate = new Promise<void>(resolve => { releaseCaption = resolve; });
    let captionRequestCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PATCH" && path.includes("/api/operator-settings")) return response({ llm: { provider: "openai", model: "vision-model", base_url: null }, captioning: { presets: [{ name: "Scene only", prompt: "Describe the portrait clearly." }] } });
      if (init?.method === "PATCH" && path.includes("/caption-format")) return response({ caption_format: "json", items: [{ ...items[0], caption_format: "json" }] });
      if (init?.method === "POST" && path.includes("/api/dataset-drafts/draft-1/caption")) {
        captionRequestCount += 1;
        const requestedId = JSON.parse(String(init.body)).item_ids[0];
        if (captionRequestCount === 2) await captionGate;
        const item = items.find(candidate => candidate.id === requestedId)!;
        return response({ items: [{ ...item, caption: `generated ${requestedId}` }], changed: 1, count: 1, operation_id: `op-${captionRequestCount}` });
      }
      if (path.includes("/api/datasets/dataset-1/drafts/active")) return response(draft);
      if (init?.method === "POST" && path.includes("/api/datasets/dataset-1/drafts")) return response({ id: "draft-1" });
      if (path.includes("/api/dataset-drafts/draft-1")) return response(draft);
      if (path.includes("/api/datasets/dataset-1/versions")) return response([{ id: "version-1", version_number: 1, name: "Imported", item_count: 2, caption_format: "text", status: "published" }]);
      if (path.includes("/api/datasets/dataset-1")) return response({ id: "dataset-1", name: "Portraits", project_name: "Studio", current_version_id: "version-1", versions: [{ id: "version-1", version_number: 1, name: "Imported", item_count: 2, caption_format: "text", status: "published" }] });
      if (path.includes("/api/dataset-versions/version-1/items")) return response(items);
      if (path.includes("/api/operator-settings/llm-models")) return response({ provider: "openai", models: [{ id: "vision-model", label: "Vision Model", description: "Vision", supports_images: true }, { id: "other-model", label: "Other Model", description: "Vision", supports_images: true }] });
      if (path.includes("/api/operator-settings")) return response({ llm: { provider: "openai", model: "vision-model", base_url: null }, captioning: { presets: [] } });
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-1" />);
    const method = await screen.findByRole("button", { name: "Caption method" });
    fireEvent.click(method);
    fireEvent.click(screen.getByRole("option", { name: "AI image captions" }));
    const prompt = screen.getByRole("textbox", { name: "Caption prompt" });
    expect(await screen.findByRole("button", { name: "Caption model" })).toHaveTextContent("Vision Model");
    const modelFilter = screen.getByRole("searchbox", { name: "Filter caption models" });
    fireEvent.change(modelFilter, { target: { value: "vision-model" } });
    fireEvent.click(screen.getByRole("button", { name: "Caption model" }));
    expect(screen.queryByRole("option", { name: /Other Model/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /Vision Model/ }));
    expect(screen.getByDisplayValue("openai")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Caption format" })).toHaveTextContent("Text");
    fireEvent.change(prompt, { target: { value: "Describe the portrait clearly." } });
    fireEvent.click(screen.getByRole("button", { name: "Generate captions" }));
    expect(await screen.findByText("1 of 2 complete")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Caption generation progress" })).toHaveAttribute("value", "1");
    expect(screen.getByRole("button", { name: "Generating 1 / 2" })).toBeDisabled();
    releaseCaption();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/dataset-drafts/draft-1/caption", expect.objectContaining({ method: "POST" })));
    const calls = fetchMock.mock.calls.filter(([path]) => String(path).includes("/api/dataset-drafts/draft-1/caption"));
    expect(calls).toHaveLength(2);
    expect(calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      expect.objectContaining({ prompt: "Describe the portrait clearly.", model: "vision-model", caption_format: "text", item_ids: ["item-1"], all: false }),
      expect.objectContaining({ prompt: "Describe the portrait clearly.", model: "vision-model", caption_format: "text", item_ids: ["item-2"], all: false }),
    ]);
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("/publish"))).toBe(false);
    expect(await screen.findByText(/Changes remain in the working draft until you publish/)).toBeInTheDocument();
    expect(await within(screen.getAllByRole("article")[0]).findByDisplayValue("generated item-1")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Caption preset name" }), { target: { value: "Scene only" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/operator-settings", expect.objectContaining({ method: "PATCH" })));
    const presetCall = fetchMock.mock.calls.find(([path, init]) => String(path).includes("/api/operator-settings") && init?.method === "PATCH");
    expect(JSON.parse(String(presetCall?.[1]?.body))).toMatchObject({ captioning: { presets: [{ name: "Scene only", prompt: "Describe the portrait clearly." }] } });
    fireEvent.click(screen.getByRole("button", { name: "Caption format" }));
    fireEvent.click(screen.getByRole("option", { name: "JSON" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/dataset-drafts/draft-1/caption-format", expect.objectContaining({ method: "PATCH" })));
    const formatCall = fetchMock.mock.calls.find(([path, init]) => String(path).includes("/caption-format") && init?.method === "PATCH");
    expect(JSON.parse(String(formatCall?.[1]?.body))).toEqual({ caption_format: "json" });
  });
});
