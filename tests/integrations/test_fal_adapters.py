import pytest

from titles_api.integrations.fal import adapters
from titles_api.integrations.fal.adapters import LoraInput, ValidationError, get_adapter
from titles_api.integrations.fal.orchestration import GridAxis, GridDefinition, GridPlanner


def test_normalize_parameters_freezes_complete_ideogram_contract():
    normalized = adapters.normalize_parameters(
        {
            "endpoint_id": "ideogram/v4/lora",
            "image_size": {"width": 1024, "height": 1536},
            "rendering_speed": "QUALITY",
            "num_images": 3,
            "lora_scale": 0.75,
        }
    )

    assert normalized == {
        "endpoint_id": "ideogram/v4/lora",
        "image_size": {"width": 1024, "height": 1536},
        "expansion_model": "Medium",
        "rendering_speed": "QUALITY",
        "acceleration": "none",
        "num_images": 3,
        "seed": None,
        "lora_scale": 0.75,
        "enable_safety_checker": True,
        "output_format": "jpeg",
    }


def test_normalize_parameters_rejects_cross_endpoint_field():
    with pytest.raises(ValidationError, match="unsupported fields"):
        adapters.normalize_parameters(
            {
                "endpoint_id": "fal-ai/krea-2/turbo/lora",
                "rendering_speed": "QUALITY",
            }
        )

def test_ideogram_request_uses_verified_fields():
    request = get_adapter("ideogram/v4/lora").build_request(
        prompt="title portrait",
        loras=[LoraInput("https://example.invalid/model.safetensors")],
        parameters={"rendering_speed": "QUALITY", "image_size": {"width": 1024, "height": 1536}},
    )
    assert request["expansion_model"] == "Medium"
    assert request["sync_mode"] is False
    assert "guidance_scale" not in request


def test_krea_rejects_nonexistent_cfg_field_only_when_not_part_of_request_contract():
    adapter = get_adapter("fal-ai/krea-2/turbo/lora")
    request = adapter.build_request(prompt="portrait", loras=[], parameters={"enable_prompt_expansion": True})
    assert request["output_format"] == "png"
    with pytest.raises(ValidationError):
        adapter.build_request(prompt="portrait", loras=[], parameters={"acceleration": "high"})
    with pytest.raises(ValidationError):
        adapter.build_request(prompt="portrait", loras=[], parameters={"guidance_scale": 7.5})


def test_grid_checkpoint_axis_resolves_step_to_lora_path():
    planner = GridPlanner(lambda step: (f"checkpoint-{step}", f"https://example.invalid/{step}.safetensors"))
    definition = GridDefinition(
        endpoint_id="fal-ai/krea-2/turbo/lora",
        row_axis=GridAxis("checkpoint_step", (1000, 2000)),
        column_axis=GridAxis("lora_scale", (0.8, 1.0)),
        base_parameters={},
    )
    tasks = planner.plan(definition, prompt_id="p1", prompt="portrait", checkpoint_id="initial", lora_path="initial")
    assert len(tasks) == 4
    assert tasks[-1].checkpoint_id == "checkpoint-2000"
    assert tasks[-1].request["loras"] == [{"path": "https://example.invalid/2000.safetensors", "scale": 1.0}]
    assert all(task.request["num_images"] == 1 for task in tasks)


def test_ideogram_custom_dimensions_are_multiples_of_16():
    with pytest.raises(ValidationError):
        get_adapter("ideogram/v4/lora").build_request(
            prompt="portrait", loras=[], parameters={"image_size": {"width": 1025, "height": 1024}}
        )
