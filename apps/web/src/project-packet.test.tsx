import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { formatCanonicalTimestamp, LatestTrainingRun } from "./screens-core";

afterEach(cleanup);

describe("project packet timestamp", () => {
  it("renders a deterministic UTC training-run timestamp", () => {
    expect(formatCanonicalTimestamp("2026-07-12T08:09:10.456+08:00")).toBe("2026-07-12 00:09:10 UTC");
  });

  it("does not invent a time when the API value is absent", () => {
    expect(formatCanonicalTimestamp(null)).toBe("Timestamp unavailable");
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
