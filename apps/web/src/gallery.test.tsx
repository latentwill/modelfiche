import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AssetGrid, galleryHash, galleryOpenHref, isGalleryShortcutTarget, safeReturnTarget } from "./screens-assets";

vi.mock("./asset-image", () => ({ AssetImage: ({ alt }: { alt: string }) => <img alt={alt} /> }));

describe("gallery navigation contracts", () => {
  it("preserves filters, pagination, context and exact safe origin", () => {
    const href = galleryHash({ project_id: "p1", model_id: "m1", dataset_id: "d1", category: "eval_output", kind: "image", decision: "approved", rating: "5", q: "portrait", include_dataset_assets: true, sort: "origin_time_asc" }, 2, 100, "a1", "project/p1?tab=evals&page=3");
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toMatchObject({ project: "p1", model: "m1", dataset: "d1", category: "eval_output", page: "2", pageSize: "100", asset: "a1", return: "project/p1?tab=evals&page=3", sort: "origin_time_asc" });
    expect(safeReturnTarget("https://evil.test")).toBe("");
    expect(safeReturnTarget("eval/run-1?page=2")).toBe("eval/run-1?page=2");
  });

  it("shows canonical asset badges and separate model/run metadata", () => {
    render(<AssetGrid assets={[{ id: "a1", name: "sample.png", origin_type: "SAMPLE", model_name: "Checkpoint model", run_name: "Training run 4" }]} />);
    expect(screen.getByText("SAMPLE")).toBeInTheDocument();
    expect(screen.getByText(/Checkpoint model · Run: Training run 4/)).toBeInTheDocument();
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

  it("suppresses gallery shortcuts while the operator is typing", () => {
    const input = document.createElement("input");
    const button = document.createElement("button");
    expect(isGalleryShortcutTarget(input)).toBe(true);
    expect(isGalleryShortcutTarget(button)).toBe(false);
  });
});
