import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SampleViewerScreen, forwardSampleComparisonWheel, sampleComparisonColumns, sampleComparisonRows } from "./screens-wireframe";

vi.mock("./asset-image", () => ({
  AssetImage: ({ assetRevisionId, alt = "" }: { assetRevisionId: string; alt?: string }) => <img src={`/asset/${assetRevisionId}`} alt={alt} />,
}));

const response = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }));

function sample(checkpoint: number, prompt = 0) {
  return {
    id: `sample-${checkpoint}-${prompt}`,
    asset_id: `asset-${checkpoint}-${prompt}`,
    asset_revision_id: `asset-${checkpoint}-${prompt}`,
    checkpoint_id: `checkpoint-${checkpoint}`,
    checkpoint_filename: `portrait_${String(checkpoint).padStart(9, "0")}.safetensors`,
    checkpoint_state: "available",
    checkpoint_readiness: { local: { status: "remote" } },
    model_version_id: `version-${checkpoint}`,
    model_version_name: `Portrait ${checkpoint}`,
    training_step: checkpoint,
    sample_identity: `sample-${prompt}`,
    sample_label: `Prompt ${prompt + 1}`,
    sample_ordinal: prompt,
    rating: null,
    decision: null,
  };
}

beforeEach(() => vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null), setItem: vi.fn(), removeItem: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); location.hash = ""; });

describe("SampleViewerScreen", () => {
  it("builds prompt rows by true checkpoint-step columns", () => {
    const data = [sample(250), sample(500)];
    expect(sampleComparisonColumns(data).map(column => [column.id, column.label])).toEqual([
      ["checkpoint-250", "Step 250"],
      ["checkpoint-500", "Step 500"],
    ]);
    const rows = sampleComparisonRows(data);
    expect(rows).toHaveLength(1);
    expect(Object.keys(rows[0].cells)).toEqual(["checkpoint-250", "checkpoint-500"]);
  });

  it("forwards vertical wheel input from a horizontally scrollable sample matrix", () => {
    const scrollBy = vi.fn();
    const preventDefault = vi.fn();
    vi.stubGlobal("scrollBy", scrollBy);
    forwardSampleComparisonWheel({ deltaX: 0, deltaY: 320, currentTarget: { clientHeight: 100, scrollHeight: 100 } as HTMLElement, preventDefault });
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(scrollBy).toHaveBeenCalledWith({ top: 320 });

    forwardSampleComparisonWheel({ deltaX: 320, deltaY: 0, currentTarget: { clientHeight: 100, scrollHeight: 100 } as HTMLElement, preventDefault });
    forwardSampleComparisonWheel({ deltaX: 0, deltaY: 320, currentTarget: { clientHeight: 100, scrollHeight: 200 } as HTMLElement, preventDefault });
    expect(scrollBy).toHaveBeenCalledOnce();
  });


  it("restores URL selection, preserves return context, and bounds mounted review rows", async () => {
    const samples = Array.from({ length: 7 }, (_, column) => Array.from({ length: 20 }, (_, prompt) => sample((column + 1) * 250, prompt))).flat();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), location.origin);
      const path = `${url.pathname}${url.search}`;
      if (path === "/api/runs/run-1") return response({ id: "run-1", name: "Portrait training", project_id: "project-1" });
      if (path === "/api/runs/run-1/samples?limit=1000") return response(samples);
      throw new Error(`Unexpected request: ${path}`);
    }));
    location.hash = "#/samples/run-1?checkpoints=checkpoint-500&return=model%2Fmodel-1";

    const view = render(<SampleViewerScreen runId="run-1" />);
    expect(view.container.querySelector(".comparison-viewer.vela-focus-workspace")).toBeInTheDocument();
    const selected = await screen.findByRole("button", { name: /Step 500/ });
    await waitFor(() => expect(selected).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByRole("button", { name: /Step 250/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("link", { name: "Close sample viewer" })).toHaveAttribute("href", "#/model/model-1?project=project-1");
    expect(view.container.querySelectorAll(".sample-comparison-table tbody tr")).toHaveLength(8);
    expect(view.container.querySelectorAll(".sample-comparison-table thead th")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: /Step 250/ }));
    await waitFor(() => expect(location.hash).toContain("checkpoints=checkpoint-500%2Ccheckpoint-250"));
    expect(screen.getByText("2/6 selected")).toBeInTheDocument();
    expect(view.container.querySelectorAll(".sample-comparison-table thead th")).toHaveLength(3);
  });

  it("shows one shared persisted decision editor instead of controls in every cell", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin);
      const path = `${url.pathname}${url.search}`;
      if (path === "/api/runs/run-1") return response({ id: "run-1", name: "Portrait training", project_id: "project-1" });
      if (path === "/api/runs/run-1/samples?limit=1000") return response([{ ...sample(250), rating: 4, decision: "approved", reviewed_by: "Ada", reviewed_at: "2026-07-16T10:00:00Z" }]);
      if (path === "/api/reviews" && init?.method === "POST") return response({ decision: "approved", rating: 4, profile_name: "Ada", created_at: "2026-07-16T10:00:00Z" }, 201);
      throw new Error(`Unexpected request: ${path}`);
    }));

    const view = render(<SampleViewerScreen runId="run-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Review Prompt 1 at Step 250" }));
    const review = screen.getByRole("complementary", { name: "Review selected sample" });
    expect(within(review).getByRole("button", { name: "Production decision" })).toHaveTextContent("Approved");
    expect(within(review).getByText(/approved by Ada/i)).toBeInTheDocument();
    expect(view.container.querySelectorAll(".sample-comparison-cell")).toHaveLength(2);
    expect(view.container.querySelectorAll(".sample-review-controls")).toHaveLength(1);
  });
});
