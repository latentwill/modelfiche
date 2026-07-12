import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { formatCanonicalTimestamp, latestTrainingRun, LatestTrainingRun, PROJECT_IMAGE_COLLECTION_ORDER } from "./screens-core";

afterEach(cleanup);

describe("project packet timestamp", () => {
  it("places Dataset images left and Training samples in the middle", () => {
    expect(PROJECT_IMAGE_COLLECTION_ORDER).toEqual(["Dataset images", "Training samples", "Eval outputs"]);
  });
  it("renders a deterministic UTC training-run timestamp", () => {
    expect(formatCanonicalTimestamp("2026-07-12T08:09:10.456+08:00")).toBe("2026-07-12 00:09:10 UTC");
  });

  it("does not invent a time when the API value is absent", () => {
    expect(formatCanonicalTimestamp(null)).toBe("Timestamp unavailable");
  });

  it("selects latest deterministically from canonical run timestamps", () => {
    expect(latestTrainingRun([
      { id: "older", created_at: "2026-07-10T00:00:00Z" },
      { id: "latest", started_at: "2026-07-12T00:00:00Z" },
      { id: "middle", completed_at: "2026-07-11T00:00:00Z" },
    ])?.id).toBe("latest");
  });

  it("distinguishes empty, failed, and completed result states", () => {
    const empty = render(<LatestTrainingRun projectId="project-1" runs={[]} loading={false} />);
    expect(empty.getByText("No training runs yet")).toBeInTheDocument();
    empty.unmount();

    const failed = render(<LatestTrainingRun projectId="project-1" loading={false} runs={[{ id: "run-failed", name: "Broken", status: "failed", created_at: "2026-07-12T00:00:00Z" }]} />);
    expect(failed.getByRole("alert")).toHaveTextContent("Training run failed");
    expect(failed.getByRole("link", { name: /Broken/ })).toHaveAttribute("href", "#/run/run-failed");
    failed.unmount();

    const completed = render(<LatestTrainingRun projectId="project-1" loading={false} runs={[{ id: "run-ok", name: "Finished", status: "completed", created_at: "2026-07-12T00:00:00Z", result_model_id: "model-1", result_model_name: "vcribb" }]} />);
    expect(completed.getByRole("link", { name: "Open result model: vcribb" })).toHaveAttribute("href", "#/model/model-1");
  });
});
