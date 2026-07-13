import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AssetGrid, galleryHash, galleryOpenHref, galleryPreviewGeometry, isGalleryShortcutTarget, safeReturnTarget } from "./screens-assets";
import assetsSource from "./screens-assets.tsx?raw";

vi.mock("./asset-image", () => ({ AssetImage: ({ alt }: { alt: string }) => <img alt={alt} /> }));

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
});
