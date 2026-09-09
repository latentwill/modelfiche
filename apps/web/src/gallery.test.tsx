import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { AssetGrid, GalleryScreen, ModelScreen, galleryHash, galleryOpenHref, galleryPreviewGeometry, isGalleryShortcutTarget, safeReturnTarget } from "./screens-assets";
import assetsSource from "./screens-assets.tsx?raw";

vi.mock("./asset-image", () => ({ AssetImage: ({ alt }: { alt: string }) => <img alt={alt} /> }));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); location.hash = ""; });

describe("gallery navigation contracts", () => {
  it("preserves filters, pagination, context and exact safe origin", () => {
    const href = galleryHash({ project_id: "p1", model_id: "m1", dataset_id: "d1", category: "eval_output", kind: "image", decision: "approved", rating: "5", q: "portrait", include_dataset_assets: true, sort: "origin_time_asc" }, 2, 100, "a1", "project/p1?tab=evals&page=3");
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toMatchObject({ project: "p1", model: "m1", dataset: "d1", category: "eval_output", page: "2", pageSize: "100", asset: "a1", return: "project/p1?tab=evals&page=3", sort: "origin_time_asc" });
    expect(safeReturnTarget("https://evil.test")).toBe("");
    expect(safeReturnTarget("eval/run-1?page=2")).toBe("eval/run-1?page=2");
  });

  it("renders the canonical two-line metadata footer without entity substitution", () => {
    render(<AssetGrid assets={[{
      id: "a1", name: "sample.png", origin_type: "SAMPLE",
      metadata: {
        origin_type: "SAMPLE",
        relationships: {
          base_model: { name: "Flux" },
          model: { name: "Checkpoint model" },
          checkpoint: { name: "step 1200" },
        },
      },
    }]} />);
    expect(screen.getByText("Flux · SAMPLE")).toBeInTheDocument();
    expect(screen.getByText("Checkpoint model · step 1200")).toBeInTheDocument();
    expect(screen.getByRole("link")).not.toHaveAttribute("title");
    expect(screen.getByRole("link")).not.toHaveAttribute("data-origin");
  });

  it("labels only grid-provenance eval outputs as GRID", () => {
    render(<AssetGrid assets={[
      { id: "image", metadata: { origin_type: "EVAL", relationships: { base_model: { name: "Flux" } } } },
      { id: "grid", metadata: { origin_type: "EVAL", grid_cell_id: "cell-1", relationships: { base_model: { name: "Flux" } } } },
    ]} />);
    expect(screen.getByText("Flux · IMAGE")).toBeInTheDocument();
    expect(screen.getByText("Flux · GRID")).toBeInTheDocument();
  });


  it("shows LoRA scale without generation actions in the multi-image grid", () => {
    render(<AssetGrid assets={[{
      id: "a1", name: "generated.png", project_id: "p1",
      metadata: {
        project_id: "p1", model_id: "m1", model_version_id: "v1", checkpoint_revision_id: "r1",
        provider: "fal", endpoint: "fal-ai/krea-2/turbo/lora", prompt: "portrait",
        lora: { path: "https://example.test/model.safetensors", scale: 0.65 },
      },
    }]} />);
    expect(screen.getByText("LoRA scale · 0.65")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Generate/ })).not.toBeInTheDocument();
  });

  it("keeps gallery tiles square and responsive at boundary widths", () => {
    for (const [container, preferred, expectedReflow] of [[181, 208, true], [208, 208, false], [320, 208, false]] as const) {
      const geometry = galleryPreviewGeometry(container, preferred);
      expect(geometry).toMatchObject({ width: geometry.width, height: geometry.width, reflows: expectedReflow, overflows: false });
      expect(geometry.width).toBeLessThanOrEqual(container);
    }
  });

  it("opens sample-viewer assets in the filtered gallery with exact origin", () => {
    location.hash = "#/samples/run-1?step=400";
    const href = galleryOpenHref({ asset_id: "sample-1", project_id: "project-1", origin_type: "SAMPLE" });
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toMatchObject({ project: "project-1", category: "sample", asset: "sample-1", return: "samples/run-1?step=400" });
  });

  it("constrains Eval viewer lightboxes to the owning run and returns exactly", () => {
    location.hash = "#/eval/run-7?model=model-2";
    const href = galleryOpenHref({ asset_id: "eval-asset", project_id: "project-1", eval_run_id: "run-7", origin_type: "EVAL" });
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toMatchObject({ project: "project-1", eval: "run-7", category: "eval_output", asset: "eval-asset", return: "eval/run-7?model=model-2" });
  });
  it("opens model eval thumbnails in the gallery while keeping the eval summary link", async () => {
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const body = path.includes("/api/models/model-1")
        ? { id: "model-1", project_id: "project-1", name: "Model One", versions: [{ id: "version-1", base_model: "Flux" }] }
        : path.includes("/api/projects/project-1")
          ? { id: "project-1", title: "Project One" }
          : path.includes("/api/eval-runs")
            ? [{ id: "eval-1", name: "Image run", status: "succeeded", output_count: 1, outputs: [{ asset_id: "output-1", asset_revision_id: "output-1" }] }]
            : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));

    render(<ModelScreen id="model-1" />);

    const galleryLink = await screen.findByRole("link", { name: "Open Image run in gallery" });
    expect(galleryLink).toHaveAttribute("href", expect.stringContaining("#/gallery"));
    expect(galleryLink).toHaveAttribute("href", expect.stringContaining("asset=output-1"));
    const evalLink = screen.getByRole("link", { name: /Image run.*1 outputs/ });
    expect(evalLink).toHaveAttribute("href", "#/eval/eval-1?project=project-1");
  });

  it("constrains dataset review to its project and dataset and returns to the exact version", () => {
    location.hash = "#/dataset/dataset-9?version=version-3&page=2";
    const href = galleryOpenHref({ asset_id: "dataset-asset", project_id: "project-9", dataset_id: "dataset-9", origin_type: "DATASET" });
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toMatchObject({
      project: "project-9",
      dataset: "dataset-9",
      category: "dataset_image",
      asset: "dataset-asset",
      return: "dataset/dataset-9?version=version-3&page=2",
    });
  });

  it("suppresses gallery shortcuts while the operator is typing or choosing a filter", () => {
    const input = document.createElement("input");
    const dropdown = document.createElement("div");
    dropdown.className = "vela-dropdown";
    const dropdownButton = document.createElement("button");
    dropdown.append(dropdownButton);
    const button = document.createElement("button");
    expect(isGalleryShortcutTarget(input)).toBe(true);
    expect(isGalleryShortcutTarget(dropdownButton)).toBe(true);
    expect(isGalleryShortcutTarget(button)).toBe(false);
  });

  it("keeps Gallery and project Dropdowns while providing draft caption controls", () => {
    const gallery = assetsSource.slice(assetsSource.indexOf("export function GalleryScreen"), assetsSource.indexOf("function GalleryImageOverlay"));
    expect(gallery).not.toContain("<select");
    expect(gallery.match(/<Dropdown/g)).toHaveLength(7);
    expect(gallery).toContain('include_dataset_assets: value === "dataset_image"');
    expect(assetsSource).toContain('<Dropdown aria-label="Caption method" value={captionMethod}');
    expect(assetsSource).toContain('<Dropdown aria-label="Caption format" value={captionFormat}');
    expect(assetsSource).toContain("/caption-format");
    expect(assetsSource).toContain('aria-label="Caption model"');
    expect(assetsSource).toContain('aria-label="Caption preset"');
    expect(assetsSource).toContain('<Dropdown aria-label="Project" name="project_id" required defaultValue={projectId}');
  });
  it("contains focus and dismisses dataset image review with exact return context", async () => {
    const asset = { id: "asset-1", asset_id: "asset-1", asset_revision_id: "asset-1", project_id: "project-1", dataset_id: "dataset-1", name: "lighthouse.png", kind: "image", mime_type: "image/png", origin_type: "DATASET" };
    const caption = "{\"description\":\"Orange lighthouse with a long sentence that must remain fully readable and selectable in the single-image gallery review without an ellipsis or clipping\"}";
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", Object.create(navigator, { clipboard: { value: { writeText }, configurable: true } }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const body = path.includes("/api/gallery/context")
        ? { items: [asset], current: asset, previous: null, next: null }
        : path.includes("/api/assets/asset-1/context")
          ? { metadata: { caption_format: "json", caption }, image: { width: 640, height: 480 }, storage: [] }
          : path.includes("/api/assets/asset-1")
            ? asset
            : path.includes("/api/gallery")
              ? { items: [asset], total: 1 }
              : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
    const params = new URLSearchParams("project=project-1&dataset=dataset-1&category=dataset_image&asset=asset-1&return=dataset/dataset-1%3Fversion%3Dversion-3");

    render(<GalleryScreen projectId="project-1" params={params} />);

    const dialog = await screen.findByRole("dialog", { name: /^Image review/ });
    const visual = dialog.querySelector(".image-review-visual");
    expect(visual).toContainElement(dialog.querySelector(".image-review-prompt"));
    expect(visual).toContainElement(dialog.querySelector(".filmstrip"));
    expect(Array.from(visual?.children ?? []).map(child => child.classList[0])).toEqual(["image-review-media", "image-review-prompt", "filmstrip"]);
    expect(dialog.querySelector(".image-nav a:first-child")).toHaveTextContent("Previous");
    expect(dialog.querySelector(".image-nav a:last-child")).toHaveTextContent("Next");
    expect(dialog).toHaveTextContent("Rating");
    expect(dialog).not.toHaveTextContent("Image rating (keys 1–5)");
    expect(dialog.querySelector(".image-review-prompt")).toHaveTextContent("Orange lighthouse with a long sentence that must remain fully readable and selectable");
    const metadataFields = Array.from(dialog.querySelectorAll("dt"), field => field.textContent);
    expect(metadataFields).toEqual(expect.arrayContaining(["Dimensions", "Dataset", "Provider"]));
    expect(dialog.querySelector(".image-review-inspector")).toHaveClass("vela-copyable-metadata");
    expect(metadataFields).not.toEqual(expect.arrayContaining(["File", "Type", "Kind"]));
    await waitFor(() => expect(screen.getByRole("link", { name: "Close image review" })).toHaveFocus());
    expect(screen.getByRole("heading", { name: "Filters", hidden: true }).closest("section")).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(String(writeText.mock.calls[0]?.[0])).toContain("caption_format");
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), textarea:not([disabled])"));
    focusables.at(-1)?.focus();
    fireEvent.keyDown(focusables.at(-1)!, { key: "Tab" });
    expect(focusables[0]).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(location.hash).toBe("#/dataset/dataset-1?version=version-3");
  });
  it("retains training metadata in non-dataset image review", async () => {
    const asset = { id: "sample-1", asset_id: "sample-1", asset_revision_id: "sample-1", project_id: "project-1", name: "sample.png", kind: "image", mime_type: "image/png", origin_type: "SAMPLE" };
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const body = path.includes("/api/gallery/context")
        ? { items: [asset], current: asset, previous: null, next: null }
        : path.includes("/api/assets/sample-1/context")
          ? { metadata: { origin_type: "SAMPLE", provider: "comfyui", workflow: { name: "Krea 2 ComfyUI API workflow", url: "https://workflow.example/krea2.json" }, lora: { scale: 0.65 }, relationships: { model: { name: "Fine tune" }, checkpoint: { name: "step 1200" }, base_model: { name: "Flux" }, training_run: { name: "run-1" } } }, image: { width: 640, height: 480 }, storage: [] }
          : path.includes("/api/assets/sample-1")
            ? asset
            : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));

    render(<GalleryScreen projectId="project-1" params={new URLSearchParams("project=project-1&category=sample&asset=sample-1")} />);

    const dialog = await screen.findByRole("dialog", { name: /^Image review/ });
    const metadataFields = Array.from(dialog.querySelectorAll("dt"), field => field.textContent);
    expect(metadataFields).toEqual(expect.arrayContaining(["Model", "Model version", "Checkpoint", "Step", "Base model", "Training run", "LoRA scale", "Provider", "Workflow"]));
    expect(screen.getByRole("link", { name: "Krea 2 ComfyUI API workflow" })).toHaveAttribute("href", "https://workflow.example/krea2.json");
    expect(metadataFields).not.toContain("File");
  });
  it("routes activity image links through the canonical gallery reviewer", async () => {
    const asset = { id: "activity-image", asset_id: "activity-image", asset_revision_id: "activity-image", project_id: "project-1", name: "opaque-generated-name.png", kind: "image", mime_type: "image/png", origin_type: "EVAL" };
    const checkpoint = "hoover-3750-x-fujiwara-kaoru-3000-spherical-60-40-1debb3b021b6.safetensors";
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const body = path.includes("/api/gallery/context")
        ? { items: [asset], current: asset, previous: null, next: null }
        : path.includes("/api/assets/activity-image/context")
          ? { metadata: { origin_type: "EVAL", project_id: "project-1", model_id: "model-1", model_version_id: "version-1", checkpoint_revision_id: "revision-1", provider: "fal", endpoint: "fal-ai/krea-2/turbo/lora", prompt: "watercolor portrait", seed: 162100712, relationships: { checkpoint: { name: checkpoint } } }, image: { width: 1024, height: 1024 }, storage: [] }
          : path.includes("/api/assets/activity-image")
            ? asset
            : path.includes("/api/gallery")
              ? { items: [asset], total: 1 }
              : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
    location.hash = "#/w/workspace-1/image/activity-image?project=project-1";

    render(<App />);

    const dialog = await screen.findByRole("dialog", { name: /^Image review/ });
    await waitFor(() => expect(dialog).toHaveTextContent("IMAGE"));
    expect(dialog).toHaveTextContent(checkpoint);
    expect(dialog).not.toHaveTextContent(".png");
    expect(Array.from(dialog.querySelectorAll("dt"), field => field.textContent)).not.toContain("File");
    expect(dialog).toHaveTextContent("162100712");
    expect(Array.from(dialog.querySelectorAll("dt"), field => field.textContent)).toContain("Seed");
    expect(screen.getByRole("link", { name: "Generate" })).toBeInTheDocument();
    const deleteButton = screen.getByRole("button", { name: "Delete image" });
    const actionRow = dialog.querySelector(".image-review-action-row");
    expect(actionRow).not.toBeNull();
    expect(actionRow).toContainElement(screen.getByRole("link", { name: "Generate" }));
    expect(actionRow).toContainElement(deleteButton);
    const metadata = dialog.querySelector(".image-all-metadata");
    const rating = dialog.querySelector(".rating-control");
    expect(metadata).not.toBeNull();
    expect(rating).not.toBeNull();
    expect(metadata!.compareDocumentPosition(rating!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
