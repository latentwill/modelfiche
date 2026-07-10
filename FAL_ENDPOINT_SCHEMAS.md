# FAL LoRA Endpoint Schemas

Verified against the FAL model schemas on 2026-07-10.

Official sources:

- <https://fal.ai/models/ideogram/v4/lora/api>
- <https://fal.ai/models/fal-ai/krea-2/turbo/lora/api>

## Ideogram 4 LoRA

Endpoint ID: `ideogram/v4/lora`

| Field | Type | Required | Default / constraints | DAM source |
| --- | --- | --- | --- | --- |
| `prompt` | string | Yes | - | Prompt-set row |
| `expansion_model` | enum | No | `Medium`; `None`, `Medium`, `Large` | Eval/Grid control |
| `image_size` | enum or object | No | `square_hd`; six presets or custom width/height | Eval/Grid control |
| `rendering_speed` | enum | No | `BALANCED`; `TURBO`, `BALANCED`, `QUALITY` | Eval/Grid control |
| `acceleration` | enum | No | `none`; `none`, `low`, `regular`, `high` | Eval/Grid control |
| `num_images` | integer | No | 1-4; default 1 | Eval control; fixed 1 per Grid cell |
| `seed` | integer or null | No | Random when omitted | Eval/Grid control or Grid axis |
| `sync_mode` | boolean | No | `false` | Fixed false so output remains in request history |
| `enable_safety_checker` | boolean | No | `true` | Eval/Grid control |
| `output_format` | enum | No | `jpeg`; `jpeg`, `png` | Eval/Grid control |
| `loras` | list | No | Maximum 3 | Selected model/checkpoint plus scale control |

`image_size` custom limits: width and height 512-3840, multiples of 16.

### Ideogram LoRA item

| Field | Type | Required | Default / constraints | DAM source |
| --- | --- | --- | --- | --- |
| `path` | string | Yes | URL to Ideogram V4 `.safetensors` | Selected model or checkpoint upload URL |
| `scale` | number | No | 0-4; default 1 | Eval/Grid control or Grid axis |

## Krea 2 Turbo LoRA

Endpoint ID: `fal-ai/krea-2/turbo/lora`

| Field | Type | Required | Default / constraints | DAM source |
| --- | --- | --- | --- | --- |
| `prompt` | string | Yes | 1-5000 characters | Prompt-set row |
| `seed` | integer or null | No | Image `i` uses `seed + i` | Eval/Grid control or Grid axis |
| `image_size` | enum or object | No | `square_hd`; six presets or custom width/height | Eval/Grid control |
| `num_images` | integer | No | 1-4; default 1 | Eval control; fixed 1 per Grid cell |
| `acceleration` | enum | No | `none`; `none`, `regular` | Eval/Grid control |
| `enable_prompt_expansion` | boolean | No | `false` | Eval/Grid control |
| `sync_mode` | boolean | No | `false` | Fixed false so output remains in request history |
| `enable_safety_checker` | boolean | No | `true` | Eval/Grid control |
| `output_format` | enum | No | `png`; `jpeg`, `png` | Eval/Grid control |
| `loras` | list | No | Maximum 3 | Selected model/checkpoint plus scale control |

Krea custom `image_size` width and height are positive integers with a schema maximum of 14142.

### Krea LoRA item

| Field | Type | Required | Default / constraints | DAM source |
| --- | --- | --- | --- | --- |
| `path` | string | Yes | URL, Hugging Face repo ID, or local path | Selected model or checkpoint path |
| `scale` | number | No | 0-4; default 1 | Eval/Grid control or Grid axis |

## Grid Axes

Grid axes combine endpoint fields with DAM-owned comparison dimensions.

Common axes:

- `Prompt`: selects a prompt-set row.
- `Checkpoint step`: selects a training checkpoint and supplies its LoRA `path` to the cell request.
- `LoRA scale`: overrides `loras[0].scale` per cell.
- `Seed`: overrides `seed` per cell.
- `Model`: selects a registered model version and its LoRA path.

Ideogram-only endpoint axes:

- `Expansion model` -> `expansion_model`.
- `Image size` -> `image_size`.
- `Rendering speed` -> `rendering_speed`.
- `Acceleration` -> `acceleration`.

Krea-only endpoint axes:

- `Image size` -> `image_size`.
- `Acceleration` -> `acceleration`.
- `Prompt expansion` -> `enable_prompt_expansion`.

Fields deliberately excluded as Grid axes:

- `num_images`: fixed at one output per cell to preserve matrix semantics.
- `sync_mode`: fixed false for durable request history.
- `enable_safety_checker`: request policy, not a useful quality sweep.
- `output_format`: storage representation, not a model-quality dimension.

## Request Construction

For a checkpoint-step Grid cell, the DAM resolves the selected step before submission:

```json
{
  "prompt": "kzapata portrait, direct gaze, neutral studio sweep",
  "seed": 2201,
  "num_images": 1,
  "sync_mode": false,
  "loras": [
    {
      "path": "<stored FAL-accessible URL for checkpoint step 2000>",
      "scale": 1.0
    }
  ]
}
```

The endpoint adapter adds the selected endpoint's defaults and validates the final request against its stored schema before queue submission.
