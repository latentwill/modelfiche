import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModelScreen } from "./screens-assets";
import { ModelVersionScreen } from "./screens-model-version";

vi.mock("./asset-image", () => ({ AssetImage: () => <div data-testid="asset-image" /> }));

const json = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } }));

const version = {
  id: "version-1",
  model_id: "model-1",
  model_name: "Portrait LoRA",
  project_id: "project-1",
  name: "Portrait step 250",
  lifecycle_state: "candidate",
  artifact_type: "lora",
  artifact_format: "ai-toolkit-lora",
  method: "lora",
  compatibility: {},
  checkpoint_id: "checkpoint-250",
  checkpoint_step: 250,
  filename: "portrait_000000250.safetensors",
  run_id: "run-1",
  run_name: "Portrait training",
  checkpoint_run_id: "run-1",
  checkpoint_run_name: "Portrait training",
  dataset: {
    dataset_id: "dataset-1",
    dataset_name: "Portrait inputs",
    dataset_version_id: "dataset-version-1",
    dataset_version_name: "Initial import",
    available: true,
  },
  readiness_summary: {
    local: { status: "hydrated", reason: "A verified local copy is already available.", path: "managed/portrait.safetensors" },
    fal: { status: "not_registered", reason: "Not registered with FAL." },
  },
  artifact: { id: "asset-1", filename: "portrait_000000250.safetensors", mime_type: "application/octet-stream", sha256: "abc", size: 2048 },
};

beforeEach(() => vi.stubGlobal("localStorage", { getItem: vi.fn().mockReturnValue(null), setItem: vi.fn(), removeItem: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("model versions", () => {
  it("renders a registered version as a native link distinct from its checkpoint", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), location.origin);
      const path = `${url.pathname}${url.search}`;
      if (path === "/api/models/model-1") return json({ id: "model-1", project_id: "project-1", name: "Portrait LoRA", latest_version: version, versions: [version] });
      if (path === "/api/projects/project-1") return json({ id: "project-1", title: "Model review" });
      if (path.startsWith("/api/eval-runs?")) return json([]);
      if (path.startsWith("/api/runs?")) return json([]);
      if (path.startsWith("/api/gallery?")) return json([]);
      throw new Error(`Unexpected request: ${path}`);
    }));

    render(<ModelScreen id="model-1" />);
    const versionLinks = await screen.findAllByRole("link", { name: /Portrait step 250/ });
    expect(versionLinks).not.toHaveLength(0);
    expect(versionLinks.every(link => link.getAttribute("href") === "#/model-version/version-1?project=project-1")).toBe(true);
    expect(screen.getByRole("link", { name: "portrait_000000250.safetensors" })).toHaveAttribute("href", "#/checkpoint/checkpoint-250?project=project-1");
  });

  it("shows readiness, typed lineage, persisted production decision, and consequence gating", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), location.origin);
      const path = `${url.pathname}${url.search}`;
      if (path === "/api/model-versions/version-1") return json(version);
      if (path.startsWith("/api/reviews?")) return json([{ id: "review-1", subject_type: "model_version", subject_id: "version-1", decision: "approved", rating: 5, profile_name: "Ada Operator", created_at: "2026-07-16T10:00:00Z" }]);
      if (path.startsWith("/api/lineage?")) return json([{ id: "derived:run:checkpoint", source_type: "training_run", source_id: "run-1", source_label: "Portrait training", source_href: "#/run/run-1", relationship: "produced", target_type: "checkpoint", target_id: "checkpoint-250", target_label: "portrait_000000250.safetensors", target_href: "#/checkpoint/checkpoint-250", created_at: "2026-07-16T09:00:00Z", derived: true }]);
      throw new Error(`Unexpected request: ${path}`);
    }));

    render(<ModelVersionScreen id="version-1" />);
    expect(await screen.findByRole("heading", { name: "Portrait step 250" })).toBeInTheDocument();
    expect(screen.getByText("A verified local copy is already available.")).toBeInTheDocument();
    expect(screen.getByText("Not registered with FAL.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Checkpoint artifact" })).toHaveAttribute("href", "#/checkpoint/checkpoint-250?project=project-1");
    expect(screen.getByRole("link", { name: "Portrait inputs · Initial import" })).toHaveAttribute("href", "#/dataset/dataset-1?project=project-1");
    expect(await screen.findByText(/approved by Ada Operator/i)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Portrait training" }).map(link => link.getAttribute("href"))).toEqual(expect.arrayContaining(["#/run/run-1?project=project-1", "#/run/run-1"]));

    const archive = screen.getByRole("button", { name: "Archive registration" });
    expect(archive).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /reviewed the checkpoint, readiness, lineage, and downstream impact/i }));
    await waitFor(() => expect(archive).toBeEnabled());
  });

  it("presents embedding type, format, method, and compatibility as a first-class artifact contract", async () => {
    const embeddingVersion = {
      ...version,
      name: "Airbrush DSCI step 8000",
      artifact_type: "embedding",
      artifact_format: "kef-qwen-dsci-v1",
      method: "dsci",
      compatibility: {
        "qwen-image": { status: "observed" },
        tensor_metadata: { shape: [5, 3584], token_count: 5 },
      },
    };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), location.origin);
      const path = `${url.pathname}${url.search}`;
      if (path === "/api/model-versions/version-1") return json(embeddingVersion);
      if (path.startsWith("/api/reviews?")) return json([]);
      if (path.startsWith("/api/lineage?")) return json([]);
      throw new Error(`Unexpected request: ${path}`);
    }));

    render(<ModelVersionScreen id="version-1" />);
    expect(await screen.findByRole("heading", { name: "Airbrush DSCI step 8000" })).toBeInTheDocument();
    const contract = screen.getByRole("heading", { name: "Artifact contract" }).closest("section");
    expect(contract).not.toBeNull();
    expect(within(contract!).getByText("embedding")).toBeInTheDocument();
    expect(within(contract!).getByText("kef-qwen-dsci-v1")).toBeInTheDocument();
    expect(within(contract!).getByText("dsci")).toBeInTheDocument();
    fireEvent.click(within(contract!).getByText("Compatibility and tensor metadata"));
    expect(within(contract!).getByText(/qwen-image/)).toBeInTheDocument();
  });
});
