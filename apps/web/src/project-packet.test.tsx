import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { collectDroppedLocalFiles, formatCanonicalTimestamp, ImportScreen, latestTrainingRun, LatestTrainingRun, PROJECT_IMAGE_COLLECTION_ORDER, ProjectScreen } from "./screens-core";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("project packet timestamp", () => {
  it("places Dataset images left and Training samples in the middle", () => {
    expect(PROJECT_IMAGE_COLLECTION_ORDER).toEqual(["Dataset images", "Training samples", "Grid outputs"]);
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
    expect(failed.getByRole("link", { name: /Broken/ })).toHaveAttribute("href", "#/run/run-failed?project=project-1");
    failed.unmount();

    const completed = render(<LatestTrainingRun projectId="project-1" loading={false} runs={[{ id: "run-ok", name: "Finished", status: "completed", created_at: "2026-07-12T00:00:00Z", result_model_id: "model-1", result_model_name: "vcribb" }]} />);
    expect(completed.getByRole("link", { name: "Result model vcribb" })).toHaveAttribute("href", "#/model/model-1?project=project-1");
    expect(completed.getByRole("link", { name: "View all training runs" })).toHaveAttribute("href", "#/runs?project=project-1");
  });
});

describe("project dataset and editor actions", () => {
  it("links an empty project dataset panel to dataset creation with project context", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/api/projects/project-1")) return response({ id: "project-1", title: "Portraits", trigger_words: [] });
      if (path.includes("/api/datasets")) return response([]);
      if (path.includes("/api/runs") || path.includes("/api/models") || path.includes("/api/eval-runs") || path.includes("/api/gallery")) return response([]);
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    const view = render(<ProjectScreen id="project-1" />);
    expect(await view.findByRole("link", { name: "Create a dataset" })).toHaveAttribute("href", "#/transfers?mode=import&project=project-1");
    expect(view.getByRole("link", { name: "Edit project" })).toHaveAttribute("href", "#/project/project-1?edit=1");
  });

  it("presents and saves the project title in the reachable editor", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PATCH" && path.includes("/api/projects/project-1")) return response({ id: "project-1", title: "Updated title", trigger_words: [] });
      if (path.includes("/api/projects/project-1")) return response({ id: "project-1", title: "Original title", description: "", trigger_words: [] });
      if (path.includes("/api/runs") || path.includes("/api/models") || path.includes("/api/eval-runs") || path.includes("/api/datasets") || path.includes("/api/gallery")) return response([]);
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
    const view = render(<ProjectScreen id="project-1" editing />);
    const title = await view.findByDisplayValue("Original title");
    await waitFor(() => expect(title).toHaveFocus());
    fireEvent.change(title, { target: { value: "Updated title" } });
    fireEvent.submit(view.getByRole("button", { name: "Save project" }).closest("form")!);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/projects/project-1", expect.objectContaining({ method: "PATCH" })));
    const patch = fetchMock.mock.calls.find(([path, init]) => String(path).includes("/api/projects/project-1") && init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({ title: "Updated title" });
  });
});

describe("local folder drag and drop", () => {
  it("recursively preserves entry paths", async () => {
    const file = new File(["pixels"], "image.png", { type: "image/png" });
    const fileEntry = { isFile: true, isDirectory: false, name: "image.png", file: (resolve: (value: File) => void) => resolve(file) };
    let reads = 0;
    const readEntries = vi.fn((resolve: (entries: typeof fileEntry[]) => void) => resolve(reads++ === 0 ? [fileEntry] : []));
    const directoryEntry = {
      isFile: false,
      isDirectory: true,
      name: "portraits",
      createReader: () => ({ readEntries }),
    };
    const dataTransfer = {
      items: [{ webkitGetAsEntry: () => directoryEntry }],
      files: [],
    } as unknown as DataTransfer;
    await expect(collectDroppedLocalFiles(dataTransfer)).resolves.toEqual([{ file, path: "portraits/image.png" }]);
    expect(readEntries).toHaveBeenCalledTimes(2);
  });
});

describe("import workflow routing", () => {
  it("isolates the S3 workflow from local folder controls", () => {
    const view = render(<ImportScreen source="s3" initialProject="project-7" />);
    expect(view.getByRole("heading", { name: "Import from S3 storage" })).toBeInTheDocument();
    expect(view.getByPlaceholderText("training-runs/2026-07/")).toBeInTheDocument();
    expect(view.container.querySelector('input[type="file"]')).not.toBeInTheDocument();
    expect(view.queryByText("Choose a folder or drop it here. Nested paths are preserved.")).not.toBeInTheDocument();
  });

  it("isolates the local workflow from S3 browsing controls", () => {
    const view = render(<ImportScreen source="local" initialProject="project-7" />);
    expect(view.getByRole("heading", { name: "Import from local folder" })).toBeInTheDocument();
    expect(view.container.querySelector('input[type="file"]')).toBeInTheDocument();
    expect(view.queryByPlaceholderText("training-runs/2026-07/")).not.toBeInTheDocument();
    expect(view.queryByText("S3 / S3-compatible source")).not.toBeInTheDocument();
    expect(view.queryByRole("button", { name: "Browse" })).not.toBeInTheDocument();
  });

  it("uses the legacy route as a chooser while preserving project context", () => {
    const view = render(<ImportScreen initialProject="project 7" />);
    expect(view.getByRole("heading", { name: "Import data" })).toBeInTheDocument();
    expect(view.getByRole("link", { name: "Import from S3 storage" })).toHaveAttribute("href", "#/import?source=s3&project=project+7");
    expect(view.getByRole("link", { name: "Import from local folder" })).toHaveAttribute("href", "#/import?source=local&project=project+7");
  });
});
