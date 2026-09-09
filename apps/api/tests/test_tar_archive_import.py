import io
import tarfile
from datetime import datetime, timezone

from titles_api.integrations.s3.archive import TarArchiveBrowser
from titles_api.integrations.s3.detection import PrefixKind, detect_prefix
from titles_api.integrations.s3.sqlalchemy_sink import _checkpoint_step, _extract_step
from titles_api.integrations.s3.metrics import parse_training_log
from titles_api.image_provenance import normalize_fal_image_metadata


def test_tar_archive_browser_exposes_safe_training_run_members(tmp_path):
    archive_path = tmp_path / "run.tar"
    with tarfile.open(archive_path, "w") as archive:
        files = {
            "workspace/run/config.yaml": b"model: qwen-image\n",
            "workspace/run/samples/sample_step_250.png": b"image",
            "workspace/run/checkpoints/step-250.safetensors": b"weights",
        }
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mtime = datetime.now(timezone.utc).timestamp()
            archive.addfile(info, io.BytesIO(body))
        unsafe = tarfile.TarInfo("../outside.txt")
        unsafe.size = 3
        archive.addfile(unsafe, io.BytesIO(b"bad"))

    browser = TarArchiveBrowser(str(archive_path), "runpod-backups/run.tar", "etag")
    try:
        inventory = browser.inventory(browser.prefix)
        assert len(inventory.objects) == 3
        assert all("../" not in item.key for item in inventory.objects)
        detection = detect_prefix(inventory, browser.read_small)
        assert detection.kind == PrefixKind.TRAINING_RUN
        config_key = next(item.key for item in inventory.objects if item.name == "config.yaml")
        assert browser.read_small(config_key) == b"model: qwen-image\n"
    finally:
        browser.close()


def test_checkpoint_step_uses_training_steps_for_unsuffixed_versioned_final():
    assert _extract_step("fujiwara-kaoru-krea2-v001.safetensors") is None
    assert _checkpoint_step("fujiwara-kaoru-krea2-v001.safetensors", "fujiwara-kaoru-krea2-v001", 3000) == 3000
    assert _checkpoint_step("fujiwara-kaoru-krea2-v001_000002750.safetensors", "fujiwara-kaoru-krea2-v001", 3000) == 2750


def test_ai_toolkit_progress_log_yields_loss_graph_points():
    body = b"run:  31%|###1| 31/100 [02:43<03:49, lr: 7.7e-05 loss: 1.613e-01]\r"
    points = parse_training_log(body)
    assert [(point.step, point.name, point.value) for point in points] == [
        (31, "learning_rate", 0.000077),
        (31, "loss", 0.1613),
    ]


def test_fal_lora_filename_normalizes_historical_image_provenance():
    normalized = normalize_fal_image_metadata({
        "provider": "fal",
        "endpoint": "fal-ai/krea-2/turbo/lora",
        "fal_request_input": {"seed": 42, "loras": [{"path": "https://fal.media/ASuwLIjPEyiNbS0KHwU4R_titlesxyz-kzapata-krea2-v001-fal-2250.safetensors", "scale": 0.65}]},
    })
    assert normalized["inferred_model_name"] == "titlesxyz-kzapata-krea2-v001"
    assert normalized["base_model"] == "krea/Krea-2-Raw"
    assert normalized["step"] == 2250
    assert normalized["generation_settings"]["seed"] == 42
    assert normalized["lora_scale"] == 0.65
