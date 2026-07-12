import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { falAdapter, generationModelFromRow, generatorHref, normalizedParametersFromMetadata } from "./generation";
import { GenerationQueue, ImageGeneratorScreen, ModelCheckpointPicker } from "./generator";
import { api } from "./api";

vi.mock("./api", async importOriginal => {
  const original = await importOriginal<typeof import("./api")>();
  return { ...original, api: vi.fn(() => { throw new Error("network must not be called"); }) };
});

describe("shared generation contracts", () => {
  it("round-trips scoped model launch context in the generator href", () => {
    const href = generatorHref("image", { kind: "model", projectId: "project-a", modelId: "model-a", modelVersionId: "version-a" }, "asset-a");
    const params = new URLSearchParams(href.split("?", 2)[1]);
    expect(Object.fromEntries(params)).toEqual({ workflow: "image", scope: "model", project: "project-a", model: "model-a", model_version: "version-a", prefill_asset: "asset-a" });
  });

  it("derives FAL provider and endpoint exclusively from registration metadata", () => {
    const model = generationModelFromRow({ id: "version-a", model_id: "model-a", project_id: "project-a", checkpoint_revision_id: "revision-a", name: "v1", readiness: { fal_url: "https://example.test/model.safetensors", endpoint_id: "fal-ai/krea-2/turbo/lora" } });
    expect(model).toMatchObject({ provider: "fal", endpoint: "fal-ai/krea-2/turbo/lora", available: true });
  });

  it("rejects a cross-project request before provider submission", async () => {
    const model = generationModelFromRow({ id: "version-a", model_id: "model-a", project_id: "project-a", checkpoint_revision_id: "revision-a", readiness: { fal_url: "https://example.test/model.safetensors", endpoint_id: "fal-ai/krea-2/turbo/lora" } });
    await expect(falAdapter.submit({ workflow: "image", context: { kind: "project", projectId: "project-b" }, model, parameters: { prompt: "test", numImages: 1 }, clientRequestId: "request-a" })).rejects.toThrow("another project");
  });

  it("prefills supported normalized settings without submitting", () => {
    const result = normalizedParametersFromMetadata({ prompt: "portrait", negative_prompt: "blur", seed: 7, fal_request_input: { image_size: { width: 768, height: 1024 }, guidance_scale: 4.5, num_inference_steps: 28, scheduler: "normal" } });
    expect(result).toEqual(expect.objectContaining({ prompt: "portrait", negativePrompt: "blur", seed: 7, width: 768, height: 1024, guidance: 4.5, steps: 28, scheduler: "normal", numImages: 1 }));
  });

  it("supports both one and many checkpoint selection", () => {
    const models = [generationModelFromRow({ id: "v1", model_id: "m1", project_id: "p1", checkpoint_revision_id: "r1", readiness: { fal_url: "https://example.test/1", endpoint_id: "fal-ai/krea-2/turbo/lora" } }), generationModelFromRow({ id: "v2", model_id: "m2", project_id: "p1", checkpoint_revision_id: "r2", readiness: { fal_url: "https://example.test/2", endpoint_id: "fal-ai/krea-2/turbo/lora" } })];
    const onMany = vi.fn();
    const { rerender } = render(<ModelCheckpointPicker models={models} selected={[]} onChange={onMany} />);
    const picker = screen.getByLabelText("Models and checkpoints") as HTMLSelectElement;
    picker.options[0].selected = true; picker.options[1].selected = true; fireEvent.change(picker);
    expect(onMany).toHaveBeenCalledWith(["v1", "v2"]);
    const onOne = vi.fn();
    rerender(<ModelCheckpointPicker multiple={false} models={models} selected={[]} onChange={onOne} />);
    fireEvent.change(screen.getByLabelText("Models and checkpoints"), { target: { value: "v2" } });
    expect(onOne).toHaveBeenCalledWith(["v2"]);
  });

  it("does not submit an admission when the Image Generator opens", () => {
    vi.mocked(api).mockClear();
    render(<ImageGeneratorScreen params={new URLSearchParams("project=p1&scope=project&prefill_asset=a1")} />);
    expect(vi.mocked(api).mock.calls.some(([path]) => /generation-requests\/compile|fal-admissions/.test(String(path)))).toBe(false);
  });

  it("renders useful queue context and checkpoint labels from the persisted request", () => {
    render(<GenerationQueue items={[{ id: "q1", workflow: "image", project_id: "project-id", model_id: "model-id",
      model_version_ids: ["version-id"], status: "queued", progress: { completed: 0, total: 1 },
      request: { context: { projectTitle: "Portrait Project" }, model: { name: "Character LoRA", checkpointLabel: "model.safetensors · step 1200" } } }]} />);
    expect(screen.getByText("Portrait Project / Character LoRA")).toBeTruthy();
    expect(screen.getByText("model.safetensors · step 1200")).toBeTruthy();
    expect(screen.queryByText("project-id / model-id")).toBeNull();
  });
});
