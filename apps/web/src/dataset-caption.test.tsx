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
      if (path.includes("/api/dataset-drafts/draft-1/items")) return response({ items, total: items.length, limit: 50, offset: 0 });
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
    fireEvent.click(screen.getByRole("button", { name: "Review caption generation" }));
    expect(screen.getByRole("region", { name: "Caption generation review" })).toHaveTextContent("2 provider requests");
    expect(fetchMock.mock.calls.filter(([path]) => String(path).includes("/api/dataset-drafts/draft-1/caption"))).toHaveLength(0);
    fireEvent.change(prompt, { target: { value: "Describe something else." } });
    expect(screen.queryByRole("region", { name: "Caption generation review" })).not.toBeInTheDocument();
    fireEvent.change(prompt, { target: { value: "Describe the portrait clearly." } });
    fireEvent.click(screen.getByRole("button", { name: "Review caption generation" }));
    fireEvent.click(screen.getByRole("button", { name: "Generate reviewed captions" }));
    expect(await screen.findByText("1 of 2 complete")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Caption generation progress" })).toHaveAttribute("value", "1");
    expect(screen.getByRole("button", { name: "Review caption generation" })).toBeDisabled();
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

describe("dataset image action layout", () => {
  it("keeps the gallery action outside the image area", async () => {
    const item = { id: "item-layout", filename: "colorful.png", caption: "A colorful abstract composition", included: true, asset_revision_id: "asset-layout" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/datasets/dataset-layout/drafts/active")) return response(null);
      if (path.includes("/api/datasets/dataset-layout/versions")) return response([{ id: "version-layout", version_number: 1, name: "Imported", item_count: 1, caption_format: "text", status: "published" }]);
      if (path.includes("/api/datasets/dataset-layout")) return response({ id: "dataset-layout", project_id: "project-layout", name: "Layout dataset", current_version_id: "version-layout", versions: [{ id: "version-layout", version_number: 1, name: "Imported", item_count: 1, caption_format: "text", status: "published" }] });
      if (path.includes("/api/dataset-versions/version-layout/items")) return response({ items: [item], total: 1 });
      if (path.includes("/api/operator-settings")) return response({ captioning: {}, llm: {} });
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-layout" />);
    await waitFor(() => expect(document.querySelector(".caption-item img")).not.toBeNull());
    const image = document.querySelector(".caption-item img")!;
    const card = image.closest("article") as HTMLElement;
    const imageArea = card.querySelector(".caption-image") as HTMLElement;
    const action = card.querySelector(".caption-open-link") as HTMLAnchorElement;
    expect(imageArea).not.toContainElement(action);
    expect(card.querySelector(".caption-heading .caption-open-link")).toBe(action);
    expect(action).toHaveAttribute("href", expect.stringContaining("#/gallery"));
    expect(screen.getByRole("textbox", { name: "Caption for colorful.png" })).toBeInTheDocument();
  });
});

describe("dataset sub-dataset controls", () => {
  it("shows membership metadata, filters and sorts draft items, and keeps selection off the image", async () => {
    const items = [
      { id: "item-bravo", filename: "bravo.png", caption: "Bravo", included: true, position: 1, asset_revision_id: "asset-bravo", subdatasets: [{ id: "subset-bravo", name: "Bravo", key: "bravo", position: 1, membership_role: "primary" }] },
      { id: "item-alpha", filename: "alpha.png", caption: "Alpha", included: true, position: 0, asset_revision_id: "asset-alpha", subdatasets: [{ id: "subset-alpha", name: "Alpha", key: "alpha", position: 0, membership_role: "primary" }] },
    ];
    const draft = {
      id: "draft-subsets",
      items,
      subsets: [
        { id: "subset-alpha", name: "Alpha", key: "alpha", item_count: 1 },
        { id: "subset-bravo", name: "Bravo", key: "bravo", item_count: 1 },
      ],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/datasets/dataset-subsets/drafts/active")) return response(draft);
      if (path.includes("/api/dataset-drafts/draft-subsets/items")) {
        const request = new URL(path, "http://localhost");
        const subsetId = request.searchParams.get("subset_id");
        const filtered = items.filter(item => !subsetId || item.subdatasets.some(subset => subset.id === subsetId));
        const ordered = request.searchParams.get("sort") === "subdataset" ? [...filtered].sort((left, right) => left.position - right.position) : filtered;
        return response({ items: ordered, total: ordered.length, limit: 50, offset: 0 });
      }
      if (path.includes("/api/dataset-drafts/draft-subsets")) return response(draft);
      if (path.includes("/api/datasets/dataset-subsets/versions")) return response([{ id: "version-subsets", version_number: 1, name: "Imported", item_count: 2, caption_format: "text", status: "published" }]);
      if (path.includes("/api/datasets/dataset-subsets")) return response({ id: "dataset-subsets", project_id: "project-subsets", name: "Subset dataset", current_version_id: "version-subsets", versions: [{ id: "version-subsets", version_number: 1, name: "Imported", item_count: 2, caption_format: "text", status: "published" }] });
      if (path.includes("/api/operator-settings")) return response({ captioning: {}, llm: {} });
      return response({});
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-subsets" />);
    await screen.findByRole("checkbox", { name: "Select bravo.png" });
    expect(screen.getAllByText("Sub-dataset", { selector: "dt" })).toHaveLength(2);
    const selection = screen.getByRole("checkbox", { name: "Select bravo.png" });
    const card = selection.closest("article") as HTMLElement;
    expect(card.querySelector(".caption-image")).not.toContainElement(selection);
    expect(card.querySelector(".caption-heading")).toContainElement(selection);

    fireEvent.click(screen.getByRole("button", { name: "Sort dataset images" }));
    fireEvent.click(screen.getByRole("option", { name: "Sub-dataset, then dataset order" }));
    await waitFor(() => expect(screen.getAllByRole("article").map(article => article.querySelector("strong")?.textContent)).toEqual(["alpha.png", "bravo.png"]));

    fireEvent.click(screen.getByRole("button", { name: "Filter dataset images by sub-dataset" }));
    fireEvent.click(screen.getByRole("option", { name: /Alpha · 1 images/ }));
    await waitFor(() => {
      expect(screen.getByText("alpha.png")).toBeInTheDocument();
      expect(screen.queryByText("bravo.png")).not.toBeInTheDocument();
    });
  });
});

describe("dataset pagination and immutable batch previews", () => {
  it("reaches every item, scopes selection truthfully, invalidates stale input, and renders JSON captions", async () => {
    const items = Array.from({ length: 54 }, (_, index) => ({
      id: `item-${index + 1}`,
      filename: `image-${index + 1}.png`,
      caption: index === 53
        ? JSON.stringify({ description: "An orange lighthouse beside a calm sea", objects: ["lighthouse", "sea"] })
        : `caption ${index + 1}`,
      caption_format: index === 53 ? "text" : "text",
      included: true,
      asset_revision_id: `asset-${index + 1}`,
    }));
    const draft = { id: "draft-54", base_version_id: "version-54", items };
    const appliedBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/api/dataset-drafts/draft-54/operations/preview") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        const targetIds = body.all ? items.map(item => item.id) : body.item_ids;
        return response({ matched: targetIds.length, changed: targetIds.length, changes: [], target_item_ids: targetIds, preview_token: `token-${body.parameters.word}` });
      }
      if (path.endsWith("/api/dataset-drafts/draft-54/operations") && init?.method === "POST") {
        appliedBodies.push(JSON.parse(String(init.body)));
        return response({ changed: 4 });
      }
      if (path.endsWith("/api/dataset-drafts/draft-54/operations")) return response([]);
      if (path.includes("/api/datasets/dataset-54/drafts/active")) return response(draft);
      if (path.includes("/api/dataset-drafts/draft-54/items")) {
        const request = new URL(path, "http://localhost");
        const offset = Number(request.searchParams.get("offset") ?? 0);
        const limit = Number(request.searchParams.get("limit") ?? 50);
        return response({ items: items.slice(offset, offset + limit), total: items.length, limit, offset });
      }
      if (path.endsWith("/api/dataset-drafts/draft-54")) return response(draft);
      if (path.includes("/api/datasets/dataset-54/versions")) return response([{ id: "version-54", version_number: 1, name: "Imported", item_count: 54, caption_format: "text", status: "published" }]);
      if (path.includes("/api/datasets/dataset-54")) return response({ id: "dataset-54", project_id: "project-54", project_name: "Studio", name: "All images", current_version_id: "version-54", versions: [{ id: "version-54", version_number: 1, name: "Imported", item_count: 54, caption_format: "text", status: "published" }] });
      if (path.includes("/api/dataset-versions/version-54/items")) return response(items);
      if (path.includes("/api/operator-settings")) return response({ captioning: {}, llm: {} });
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-54" />);
    expect(await screen.findByText("Restored the active working draft.")).toBeInTheDocument();

    expect(await screen.findByText("1–50 of 54")).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("51–54 of 54")).toBeInTheDocument();
    expect((screen.getByRole("textbox", { name: "JSON caption for image-54.png" }) as HTMLTextAreaElement).value).toContain("\"description\": \"An orange lighthouse");
    expect(screen.getByRole("img", { name: "An orange lighthouse beside a calm sea" })).toBeInTheDocument();
    expect(screen.getByLabelText("Status: JSON")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Select visible 4" }));
    expect(screen.getByText(/4 selected · showing 51–54 of 54/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Batch scope" }));
    fireEvent.click(screen.getByRole("option", { name: "Selected (4)" }));
    const word = screen.getByPlaceholderText("Trigger or tag word");
    fireEvent.change(word, { target: { value: "tok" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview add word" }));
    expect(await screen.findByRole("status", { name: "Operation preview" })).toBeInTheDocument();
    const previewCall = fetchMock.mock.calls.find(([path]) => String(path).includes("/operations/preview"));
    expect(JSON.parse(String(previewCall?.[1]?.body))).toMatchObject({ all: false, item_ids: ["item-51", "item-52", "item-53", "item-54"] });

    fireEvent.change(word, { target: { value: "updated-token" } });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Apply this preview" })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Preview add word" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply this preview" }));
    await waitFor(() => expect(appliedBodies).toHaveLength(1));
    expect(appliedBodies[0]).toMatchObject({ all: false, item_ids: ["item-51", "item-52", "item-53", "item-54"], preview_token: "token-updated-token" });

    fireEvent.click(screen.getByRole("button", { name: "Select all 54" }));
    expect(screen.getByText(/54 selected · showing 51–54 of 54/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Batch scope" }));
    expect(screen.getByRole("option", { name: "Selected (54)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Entire draft (54)" })).toBeInTheDocument();
  });
});

describe("dataset caption version switching", () => {
  const versions = [
    { id: "version-2", version_number: 2, name: "Clean captions", item_count: 1, caption_format: "text", status: "published" },
    { id: "version-1", version_number: 1, name: "Original captions", item_count: 1, caption_format: "text", status: "published" },
  ];

  it("switches between published versions while preserving an active working draft", async () => {
    const draft = { id: "draft-1", base_version_id: "version-1", items: [{ id: "draft-item", filename: "draft.png", caption: "draft caption", included: true }] };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/datasets/dataset-1/drafts/active")) return response(draft);
      if (path.includes("/api/dataset-drafts/draft-1")) return response(draft);
      if (path.includes("/api/datasets/dataset-1/versions")) return response(versions);
      if (path.includes("/api/datasets/dataset-1")) return response({ id: "dataset-1", name: "Portraits", current_version_id: "version-2", versions });
      if (path.includes("/api/dataset-versions/version-2/items")) return response({ items: [{ id: "v2-item", filename: "v2.png", caption: "clean caption", included: true }], total: 1 });
      if (path.includes("/api/dataset-versions/version-1/items")) return response({ items: [{ id: "v1-item", filename: "v1.png", caption: "original caption", included: true }], total: 1 });
      if (path.includes("/api/operator-settings")) return response({ captioning: {}, llm: {} });
      return response([]);
    }));
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-1" />);
    expect(await screen.findByText("Restored the active working draft.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Batch caption operations" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /v2 Clean captions/ }));
    expect(await screen.findByDisplayValue("clean caption")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Batch caption operations" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Draft Working copy/ })).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(screen.getByRole("button", { name: /Draft Working copy/ }));
    expect(await screen.findByDisplayValue("draft caption")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Batch caption operations" })).toBeInTheDocument();
  });

  it("clones the selected historical version into a new working draft", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/api/datasets/dataset-1/drafts/active")) return new Response(JSON.stringify({ detail: "no active working draft" }), { status: 404, headers: { "content-type": "application/json" } });
      if (init?.method === "POST" && path.includes("/api/datasets/dataset-1/drafts")) return response({ id: "draft-from-v1" });
      if (path.includes("/api/dataset-drafts/draft-from-v1")) return response({ id: "draft-from-v1", base_version_id: "version-1", items: [{ id: "draft-item", caption: "original caption" }] });
      if (path.includes("/api/datasets/dataset-1/versions")) return response(versions);
      if (path.includes("/api/datasets/dataset-1")) return response({ id: "dataset-1", name: "Portraits", current_version_id: "version-2", versions });
      if (path.includes("/api/dataset-versions/")) return response({ items: [{ id: "published-item", filename: "published.png", caption: "published caption" }], total: 1 });
      if (path.includes("/api/operator-settings")) return response({ captioning: {}, llm: {} });
      return response([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });

    render(<DatasetScreen id="dataset-1" />);
    fireEvent.click(await screen.findByRole("button", { name: /v1 Original captions/ }));
    fireEvent.click(screen.getByRole("button", { name: "Clone as new version" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/datasets/dataset-1/drafts", expect.objectContaining({ method: "POST" })));
    const cloneCall = fetchMock.mock.calls.find(([path, init]) => String(path).includes("/api/datasets/dataset-1/drafts") && init?.method === "POST");
    expect(JSON.parse(String(cloneCall?.[1]?.body))).toEqual({ base_version_id: "version-1" });
    expect(await screen.findByDisplayValue("original caption")).toBeInTheDocument();
  });
});
