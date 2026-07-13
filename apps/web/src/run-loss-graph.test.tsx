import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunScreen } from "./screens-assets";

vi.mock("./asset-image", () => ({
  AssetImage: ({ assetRevisionId, alt }: { assetRevisionId: string; alt: string }) => <img data-testid={`sample-${assetRevisionId}`} alt={alt} />,
}));

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => null } });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockRunApi(metrics: unknown, samples: unknown[] = []) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
    const path = String(input);
    const metricRecord = metrics && typeof metrics === "object" ? metrics as Record<string, unknown> : {};
    let body: unknown;
    if (path === "/api/runs/run-1") {
      body = { id: "run-1", name: "Portrait training", base_model: "Flux", trainer: "AI Toolkit", status: "completed", project_id: "", final_loss: 9.99 };
    } else if (path === "/api/runs/run-1/live") {
      body = { run_id: "run-1", status: "completed", current_step: 120, latest_loss: { name: "train/objective", step: 120, value: metricRecord.final_value }, learning_rate: { name: "learning_rate", step: 120, value: 0.0001 }, elapsed_seconds: 75, last_event_at: "2026-07-13T10:00:00Z", uploads: { uploaded: 2, pending: 1 }, sample_count: samples.length };
    } else if (path === "/api/runs/run-1/samples") {
      body = samples;
    } else if (path === "/api/runs/run-1/checkpoints" || path.startsWith("/api/lineage")) {
      body = [];
    } else if (path === "/api/runs/run-1/config") {
      body = { normalized: {}, raw: {} };
    } else if (path === "/api/runs/run-1/metrics?name=train%2Fobjective") {
      body = metrics;
    } else if (path === "/api/runs/run-1/metrics?name=learning_rate") {
      body = { run_id: "run-1", metric_name: "learning_rate", points: [], final_value: null };
    } else {
      throw new Error(`Unexpected request: ${path}`);
    }
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
}

describe("RunScreen loss metrics", () => {
  it("renders an accessible Flint loss chart and sources final loss from metrics", async () => {
    const fetchMock = mockRunApi({
      run_id: "run-1",
      metric_name: "loss",
      points: [
        { step: 3, value: 0.25, wall_time: null },
        { step: "not-a-step", value: 0.9, wall_time: null },
        { step: 1, value: 0.5, wall_time: null },
        { step: 2, value: "not-a-value", wall_time: null },
        { step: 2, value: 0.4, wall_time: null },
      ],
      final_value: 0.25,
      source_key: "metrics/loss.json",
    });

    render(<RunScreen id="run-1" />);

    const graph = await screen.findByRole("img", { name: "Loss over training step" });
    expect(graph.querySelector("canvas")).not.toBeNull();

    const metricsPanel = screen.getByRole("heading", { name: "Training metrics" }).parentElement?.parentElement;
    expect(metricsPanel).not.toBeNull();
    expect(within(metricsPanel as HTMLElement).getByText("0.25")).toBeInTheDocument();
    expect(screen.getAllByText("0.0001 (1e-4)").length).toBeGreaterThan(0);
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
    expect(screen.getByText("2/3 uploaded · 1 pending")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-1/metrics?name=train%2Fobjective", expect.any(Object));
    expect(fetchMock).not.toHaveBeenCalledWith("/api/runs/run-1/metrics?name=loss", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-1/metrics?name=learning_rate", expect.any(Object));
  });

  it("shows a clear empty state when the metric response has no usable points", async () => {
    mockRunApi({ run_id: "run-1", metric_name: "loss", final_value: null, source_key: null });

    render(<RunScreen id="run-1" />);

    expect(await screen.findByText("No loss metrics available")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Loss over training step" })).not.toBeInTheDocument();
    const metricsPanel = screen.getByRole("heading", { name: "Training metrics" }).parentElement?.parentElement;
    expect(metricsPanel).not.toBeNull();
    expect(within(metricsPanel as HTMLElement).getAllByText("train/objective").find(element => element.tagName === "SPAN")?.nextElementSibling).toHaveTextContent("Unavailable");
  });

  it("shows live projection fields and latest sample caption, step, and upload state accessibly", async () => {
    mockRunApi(
      { run_id: "run-1", metric_name: "train/objective", points: [{ step: 120, value: 0.25 }], final_value: 0.25 },
      [{ id: "sample-1", asset_revision_id: "revision-1", caption: "A studio portrait", step: 120, upload_status: "uploaded" }],
    );

    render(<RunScreen id="run-1" />);

    const summary = await screen.findByLabelText("Live training status");
    expect(within(summary).getByLabelText("Live connection: Updates stopped")).toBeInTheDocument();
    expect(within(summary).getByLabelText("Status: completed")).toBeInTheDocument();
    expect(within(summary).getByText("120")).toBeInTheDocument();
    expect(within(summary).getByText("2/3 uploaded · 1 pending")).toBeInTheDocument();
    expect(within(summary).getByText("1m 15s")).toBeInTheDocument();
    const samples = screen.getByLabelText("Latest training samples");
    expect(within(samples).getByText("A studio portrait")).toBeInTheDocument();
    expect(within(samples).getByText("Step 120 · Upload uploaded")).toBeInTheDocument();
    expect(within(samples).getByTestId("sample-revision-1")).toHaveAccessibleName("A studio portrait");
  });
});
