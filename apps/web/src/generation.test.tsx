import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { connectedFalGenerationModels, falAdapter, generationModelFromRow, generationModelUnavailableReason, generationReplayFromMetadata, generatorHref, normalizedParametersFromMetadata, pickerModeMultiple } from "./generation";
import { EndpointFields, GenerationQueue, ImageGeneratorScreen, ModelCheckpointPicker, formatQueueTimestamp, generationActivityTransition, parametersFromForm } from "./generator";
import { Form } from "./ui";
import * as apiModule from "./api";

vi.mock("./api", async importOriginal => {
  const original = await importOriginal<typeof apiModule>();
  return { ...original, api: vi.fn(() => { throw new Error("network must not be called"); }) };
});

afterEach(() => cleanup());

describe("shared generation contracts", () => {
  it("round-trips model launch context without a scope parameter", () => {
    const href = generatorHref("image", { kind: "model", projectId: "project-a", modelId: "model-a", modelVersionId: "version-a" }, "asset-a");
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toEqual({ workflow: "image", project: "project-a", model: "model-a", model_version: "version-a", prefill_asset: "asset-a" });
  });

  it("derives FAL provider and endpoint exclusively from registration metadata", () => {
    const model = generationModelFromRow({ id: "version-a", model_id: "model-a", project_id: "project-a", checkpoint_revision_id: "revision-a", name: "v1", readiness: { fal_url: "https://example.test/model.safetensors", endpoint_id: "fal-ai/krea-2/turbo/lora" } });
    expect(model).toMatchObject({ provider: "fal", endpoint: "fal-ai/krea-2/turbo/lora", available: true });
  });

  it("offers only connected FAL checkpoints in Eval and Grid selectors", () => {
    const rows = [
      { id: "connected", model_id: "model-a", project_id: "project-a", checkpoint_revision_id: "revision-a", readiness: { fal_url: "https://example.test/a", endpoint_id: "fal-ai/krea-2/turbo/lora" } },
      { id: "unregistered", model_id: "model-b", project_id: "project-a", checkpoint_revision_id: "revision-b", readiness: {} },
      { id: "other-project", model_id: "model-c", project_id: "project-b", checkpoint_revision_id: "revision-c", readiness: { fal_url: "https://example.test/c", endpoint_id: "fal-ai/krea-2/turbo/lora" } },
    ];
    expect(connectedFalGenerationModels(rows, "project-a").map(model => model.id)).toEqual(["connected"]);
    expect(connectedFalGenerationModels(rows, "project-a", "model-b")).toEqual([]);
  });

  it("rejects a cross-project request before provider submission", async () => {
    const model = generationModelFromRow({ id: "version-a", model_id: "model-a", project_id: "project-a", checkpoint_revision_id: "revision-a", readiness: { fal_url: "https://example.test/model.safetensors", endpoint_id: "fal-ai/krea-2/turbo/lora" } });
    await expect(falAdapter.submit({ workflow: "image", context: { kind: "project", projectId: "project-b" }, model, parameters: { prompt: "test", numImages: 1 }, clientRequestId: "request-a" })).rejects.toThrow("another project");
  });

  it("prefills supported normalized settings without submitting", () => {
    const result = normalizedParametersFromMetadata({ prompt: "portrait", negative_prompt: "blur", seed: 7, generation_settings: { image_size: { width: 768, height: 1024 }, guidance_scale: 4.5, num_inference_steps: 28, scheduler: "normal", loras: [{ path: "https://example.test/model.safetensors", scale: 0.65 }] } });
    expect(result).toEqual(expect.objectContaining({ prompt: "portrait", negativePrompt: "blur", seed: 7, width: 768, height: 1024, guidance: 4.5, steps: 28, scheduler: "normal", loraScale: 0.65, numImages: 1 }));
  });

  it("renders and serializes only the selected FAL endpoint fields", () => {
    const schema = {
      defaults: { image_size: "square_hd", acceleration: "none", safety: true, num_images: 1 },
      request_fields: ["prompt", "image_size", "acceleration", "safety", "num_images", "sync_mode", "loras"],
      fields: {
        image_size: { type: "image_size", options: ["square_hd", "portrait_4_3"], custom: { min: 512, max: 2048, step: 16 } },
        acceleration: { type: "enum", options: ["none", "regular"] },
        safety: { type: "boolean" },
        num_images: { type: "integer", min: 1, max: 4 },
      },
    };
    const view = render(<form data-testid="endpoint-form"><EndpointFields schema={schema} omit={["num_images"]} /></form>);
    const size = screen.getByRole("button", { name: /Aspect ratio \/ resolution/ });
    expect(view.container.querySelector("select")).toBeNull();
    fireEvent.click(size);
    expect(screen.getByRole("option", { name: "Portrait 4:3" })).toBeTruthy();
    fireEvent.click(screen.getByRole("option", { name: "Custom dimensions" }));
    fireEvent.change(screen.getByLabelText(/Custom width/), { target: { value: "768" } });
    fireEvent.change(screen.getByLabelText(/Custom height/), { target: { value: "1024" } });
    fireEvent.click(screen.getByRole("button", { name: "acceleration" }));
    fireEvent.click(screen.getByRole("option", { name: "regular" }));
    const parameters = parametersFromForm(new FormData(view.getByTestId("endpoint-form") as HTMLFormElement), schema);
    expect(parameters).toEqual({ image_size: { width: 768, height: 1024 }, acceleration: "regular", safety: true, num_images: 1, sync_mode: false });
  });

  it("supports both one and many checkpoint selection", () => {
    const models = [generationModelFromRow({ id: "v1", model_id: "m1", project_id: "p1", checkpoint_revision_id: "r1", readiness: { fal_url: "https://example.test/1", endpoint_id: "fal-ai/krea-2/turbo/lora" } }), generationModelFromRow({ id: "v2", model_id: "m2", project_id: "p1", checkpoint_revision_id: "r2", readiness: { fal_url: "https://example.test/2", endpoint_id: "fal-ai/krea-2/turbo/lora" } })];
    const onMany = vi.fn();
    const { rerender, container } = render(<ModelCheckpointPicker mode="multiple" models={models} selected={[]} onChange={onMany} />);
    const picker = screen.getByLabelText("Models and checkpoints") as HTMLSelectElement;
    expect(picker).toHaveClass("vela-multi-select");
    expect(picker).toHaveAttribute("multiple");
    picker.options[0].selected = true; picker.options[1].selected = true; fireEvent.change(picker);
    expect(onMany).toHaveBeenCalledWith(["v1", "v2"]);
    const onOne = vi.fn();
    rerender(<ModelCheckpointPicker mode="single" models={models} selected={[]} onChange={onOne} />);
    expect(container.querySelector("select")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Model and checkpoint" }));
    fireEvent.click(screen.getAllByRole("option")[2]);
    expect(onOne).toHaveBeenCalledWith(["v2"]);
  });

  it("shares fixed and axis picker semantics with concrete disable reasons", () => {
    expect(pickerModeMultiple("fixed")).toBe(false);
    expect(pickerModeMultiple("axis")).toBe(true);
    const unavailable = generationModelFromRow({ id: "v1", model_id: "m1", project_id: "p1", checkpoint_revision_id: "r1", readiness: {} });
    expect(generationModelUnavailableReason(unavailable)).toMatch(/FAL|endpoint|unavailable/i);
  });

  it("only offers replay when immutable model and provider evidence exists", () => {
    expect(generationReplayFromMetadata({ prompt: "portrait", project_id: "p1", model_id: "m1", model_version_id: "v1" })).toBeNull();
    expect(generationReplayFromMetadata({ prompt: "portrait", project_id: "p1", model_id: "m1", model_version_id: "v1", checkpoint_revision_id: "r1", provider: "fal", endpoint_id: "fal-ai/test" })).toMatchObject({
      context: { kind: "model", projectId: "p1", modelId: "m1", modelVersionId: "v1" },
      modelVersionId: "v1",
      parameters: { prompt: "portrait", numImages: 1 },
    });
  });

  it("does not submit an admission when the Image Generator opens", () => {
    vi.mocked(apiModule.api).mockClear();
    render(<ImageGeneratorScreen params={new URLSearchParams("project=p1&prefill_asset=a1")} />);
    expect(vi.mocked(apiModule.api).mock.calls.some(([path]) => /generation-requests\/compile|fal-admissions/.test(String(path)))).toBe(false);
  });

  it("loads endpoint-specific controls for the selected checkpoint", async () => {
    vi.mocked(apiModule.api).mockImplementation(async path => {
      if (path === "/api/projects?limit=100") return [{ id: "p1", title: "Project" }];
      if (path === "/api/models?project_id=p1") return [{ id: "m1", name: "Model" }];
      if (path === "/api/model-versions?project_id=p1") return [{
        id: "v1", model_id: "m1", project_id: "p1", checkpoint_revision_id: "r1", base_model: "krea/Krea-2-Raw",
        readiness: { fal_url: "https://example.test/model.safetensors", endpoint_id: "fal-ai/krea-2/turbo/lora" },
      }];
      if (path === "/api/eval-endpoints") return [{ endpoint_id: "fal-ai/krea-2/turbo/lora", name: "Krea 2 Turbo LoRA", compatible_base_model_markers: ["krea"] }];
      if (path === "/api/eval-endpoints/fal-ai/krea-2/turbo/lora/schema") return {
        defaults: { image_size: "square_hd", acceleration: "none", enable_prompt_expansion: false, output_format: "png" },
        request_fields: ["prompt", "image_size", "acceleration", "enable_prompt_expansion", "output_format", "loras", "sync_mode"],
        fields: {
          image_size: { type: "image_size", options: ["square_hd", "portrait_4_3"], custom: { min: 1, max: 14142, step: 1 } },
          acceleration: { type: "enum", options: ["none", "regular"] },
          enable_prompt_expansion: { type: "boolean" },
          output_format: { type: "enum", options: ["jpeg", "png"] },
        },
      };
      if (path === "/api/generation-queue") return [];
      throw new Error(`unexpected request: ${path}`);
    });
    const view = render(<ImageGeneratorScreen params={new URLSearchParams("project=p1&model_version=v1")} />);
    await waitFor(() => {
      expect(view.container.querySelector('[aria-label="FAL endpoint"]')).toHaveTextContent("Krea 2 Turbo LoRA");
      expect(view.container.querySelector('[aria-label="Aspect ratio / resolution"]')).toHaveTextContent("Square HD");
      expect(view.container.querySelector('[aria-label="acceleration"]')).toHaveTextContent("none");
      expect(view.container.querySelector('[aria-label="enable prompt expansion"]')).toHaveTextContent("Disabled");
      expect(view.container.querySelector('[aria-label="output format"]')).toHaveTextContent("png");
    });
    const prompt = screen.getByRole("textbox", { name: "Prompt" });
    fireEvent.input(prompt, { target: { value: "A usable image prompt" } });
    expect(prompt).toHaveValue("A usable image prompt");
    await waitFor(() => {
      expect(view.container.querySelector('[aria-label="FAL endpoint"]')).toHaveTextContent("Krea 2 Turbo LoRA");
      expect(view.container.querySelector('button[type="submit"]')).toBeEnabled();
    });
    expect(screen.queryByRole("region", { name: /consequence review/i })).toBeNull();
    view.unmount();
    vi.mocked(apiModule.api).mockImplementation(() => { throw new Error("network must not be called"); });
  });

  it("keeps a repeatable generation form editable after each submission", async () => {
    const prompts: string[] = [];
    const view = render(<Form repeatable submit="Generate image" onSubmit={form => { prompts.push(String(form.get("prompt"))); }}><textarea aria-label="Prompt" name="prompt" defaultValue="first" /></Form>);
    fireEvent.click(view.container.querySelector('button[type="submit"]')!);
    await waitFor(() => expect(prompts).toEqual(["first"]));
    expect(view.container.querySelector('button[type="submit"]')).toBeEnabled();
    fireEvent.change(view.container.querySelector('textarea[name="prompt"]')!, { target: { value: "second" } });
    fireEvent.click(view.container.querySelector('button[type="submit"]')!);
    await waitFor(() => expect(prompts).toEqual(["first", "second"]));
  });

  it("detects a generated image completion exactly once for activity refresh", () => {
    expect(generationActivityTransition({}, [{ id: "q1", status: "completed" }]).changed).toBe(false);
    const completed = generationActivityTransition({ q1: "running" }, [{ id: "q1", status: "completed" }]);
    expect(completed).toEqual({ statuses: { q1: "completed" }, changed: true });
    expect(generationActivityTransition(completed.statuses, [{ id: "q1", status: "completed" }]).changed).toBe(false);
    expect(generationActivityTransition({ q2: "running" }, [{ id: "q2", status: "failed" }]).changed).toBe(false);
  });

  it("explains incomplete image prerequisites and blocks generation", () => {
    const { container } = render(<ImageGeneratorScreen params={new URLSearchParams()} />);
    expect(screen.getByText("Select a project.")).toBeTruthy();
    expect(screen.queryByLabelText("Scope")).toBeNull();
    expect(container.querySelector('button[type="submit"]')).toBeDisabled();
    expect(screen.queryByRole("region", { name: /consequence review/i })).toBeNull();
  });


  it("renders useful queue context and checkpoint labels from the persisted request", () => {
    render(<GenerationQueue items={[{ id: "q1", workflow: "image", project_id: "project-id", model_id: "model-id",
      model_version_ids: ["version-id"], status: "queued", progress: { completed: 0, total: 1 },
      request: { context: { projectTitle: "Portrait Project" }, model: { name: "Character LoRA", checkpointLabel: "model.safetensors · step 1200" } } }]} />);
    expect(screen.getByText("Portrait Project / Character LoRA")).toBeTruthy();
    expect(screen.getByText("model.safetensors · step 1200")).toBeTruthy();
    expect(screen.queryByText("project-id / model-id")).toBeNull();
  });
  it("links grid queue rows to the visual grid result", () => {
    render(<GenerationQueue items={[{ id: "queue-1", workflow: "grid", project_id: "project-1", grid_definition_id: "grid-1",
      model_version_ids: [], status: "completed", progress: { completed: 1, total: 1 } }]} />);
    expect(screen.getByRole("link", { name: "Grid" })).toHaveAttribute("href", "#/grid/grid-1?project=project-1");
  });
  it("shows the newest queued work first", () => {
    render(<GenerationQueue items={[
      { id: "older", workflow: "image", project_id: "project", model_version_ids: ["v1"], status: "completed", progress: { completed: 1, total: 1 }, created_at: "2026-07-20T01:02:03Z" },
      { id: "newest", workflow: "image", project_id: "project", model_version_ids: ["v2"], status: "queued", progress: { completed: 0, total: 1 }, created_at: "2026-07-21T01:02:03Z" },
    ]} />);
    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("2026-07-21 01:02:03 UTC");
    expect(rows[2]).toHaveTextContent("2026-07-20 01:02:03 UTC");
    expect(screen.getAllByText("Project unavailable")).toHaveLength(2);
    expect(screen.queryByText("project")).not.toBeInTheDocument();
    expect(formatQueueTimestamp("2026-07-21T01:02:03Z")).toBe("2026-07-21 01:02:03 UTC");
  });
  it("retries failed queue work directly", async () => {
    const reload = vi.fn();
    vi.mocked(apiModule.api).mockResolvedValueOnce({});
    render(<GenerationQueue onRetry={reload} items={[{ id: "failed-1", workflow: "image", project_id: "project", model_version_ids: ["v1"], status: "failed", error: "provider timeout" }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry failed work" }));
    await waitFor(() => expect(apiModule.api).toHaveBeenCalledWith("/api/generation-queue/failed-1/retry", expect.objectContaining({ method: "POST" })));
    expect(reload).toHaveBeenCalled();
    vi.mocked(apiModule.api).mockImplementation(() => { throw new Error("network must not be called"); });
  });


});
