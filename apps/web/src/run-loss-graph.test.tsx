import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunScreen } from "./screens-assets";

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => null } });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockRunApi(metrics: unknown) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
    const path = String(input);
    let body: unknown;
    if (path === "/api/runs/run-1") {
      body = { id: "run-1", name: "Portrait training", base_model: "Flux", trainer: "AI Toolkit", status: "completed", project_id: "", final_loss: 9.99 };
    } else if (path === "/api/runs/run-1/samples" || path === "/api/runs/run-1/checkpoints" || path.startsWith("/api/lineage")) {
      body = [];
    } else if (path === "/api/runs/run-1/config") {
      body = { normalized: {}, raw: {} };
    } else if (path === "/api/runs/run-1/metrics?name=loss") {
      body = metrics;
    } else {
      throw new Error(`Unexpected request: ${path}`);
    }
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
}

describe("RunScreen loss metrics", () => {
  it("renders a sorted accessible SVG loss graph and sources final loss from metrics", async () => {
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
    const path = graph.querySelector("path[data-loss-path]");
    expect(path).not.toBeNull();
    expect(path?.getAttribute("d")).toMatch(/^M 24 /);
    expect(path?.getAttribute("d")).not.toMatch(/NaN|Infinity/);

    const metricsPanel = screen.getByRole("heading", { name: "Training metrics" }).parentElement?.parentElement;
    expect(metricsPanel).not.toBeNull();
    expect(within(metricsPanel as HTMLElement).getByText("0.25")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-1/metrics?name=loss", expect.any(Object));
  });

  it("shows a clear empty state when the metric response has no usable points", async () => {
    mockRunApi({ run_id: "run-1", metric_name: "loss", final_value: null, source_key: null });

    render(<RunScreen id="run-1" />);

    expect(await screen.findByText("No loss metrics available")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Loss over training step" })).not.toBeInTheDocument();
    const metricsPanel = screen.getByRole("heading", { name: "Training metrics" }).parentElement?.parentElement;
    expect(metricsPanel).not.toBeNull();
    expect(within(metricsPanel as HTMLElement).getByText("Final loss").previousElementSibling).toHaveTextContent("Unavailable");
  });
});
