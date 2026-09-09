import hashlib
import io
import json
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker import export_jobs
from titles_worker.export_jobs import ExportPackageHandler, _json_default


def _ids(rows):
    return {row["id"] for row in rows}


def _graph(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'export.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        workspace = models.Workspace(name="Studio")
        session.add(workspace)
        session.flush()
        profile = models.UserProfile(workspace_id=workspace.id, display_name="Operator")
        project = models.Project(workspace_id=workspace.id, title="Primary")
        other_project = models.Project(workspace_id=workspace.id, title="Other")
        session.add_all([profile, project, other_project])
        session.flush()

        dataset = models.Dataset(project_id=project.id, name="Faces")
        session.add(dataset)
        session.flush()
        dataset_version = models.DatasetVersion(dataset_id=dataset.id, version_number=1, name="v1")
        sibling_dataset_version = models.DatasetVersion(dataset_id=dataset.id, version_number=2, name="v2")
        session.add_all([dataset_version, sibling_dataset_version])
        session.flush()
        dataset_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="image", name="train.png")
        sibling_dataset_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="image", name="sibling.png")
        session.add_all([dataset_asset, sibling_dataset_asset])
        session.flush()
        dataset_item = models.DatasetItem(dataset_version_id=dataset_version.id, asset_id=dataset_asset.id, caption="portrait", position=0)
        sibling_dataset_item = models.DatasetItem(dataset_version_id=sibling_dataset_version.id, asset_id=sibling_dataset_asset.id, caption="other", position=0)
        session.add_all([dataset_item, sibling_dataset_item])

        run = models.TrainingRun(
            project_id=project.id, dataset_version_id=dataset_version.id, name="Train", trainer="fal",
            normalized_config={"steps": 1000},
        )
        session.add(run)
        session.flush()
        stage = models.TrainingStage(run_id=run.id, name="LoRA", config={"rank": 16})
        checkpoint_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="model", name="step-1000.safetensors")
        sample_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="image", name="sample.png")
        session.add_all([stage, checkpoint_asset, sample_asset])
        session.flush()
        checkpoint = models.Checkpoint(run_id=run.id, stage_id=stage.id, step=1000, asset_id=checkpoint_asset.id)
        session.add(checkpoint)
        session.flush()
        sample = models.Sample(run_id=run.id, checkpoint_id=checkpoint.id, asset_id=sample_asset.id, step=1000, prompt="portrait")
        session.add(sample)

        model = models.Model(project_id=project.id, name="Portrait LoRA")
        session.add(model)
        session.flush()
        model_version = models.ModelVersion(model_id=model.id, checkpoint_id=checkpoint.id, name="candidate")
        session.add(model_version)

        prompt_set = models.PromptSet(project_id=project.id, name="Identity")
        session.add(prompt_set)
        session.flush()
        prompt = models.Prompt(prompt_set_id=prompt_set.id, text="studio portrait", position=0)
        session.add(prompt)
        session.flush()
        definition = models.EvalDefinition(
            project_id=project.id, name="Identity eval", endpoint="ideogram/v4/lora",
            model_version_id=model_version.id, prompt_set_id=prompt_set.id,
        )
        session.add(definition)
        session.flush()
        eval_run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint.id, status="succeeded")
        sibling_eval_run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint.id, status="succeeded")
        session.add_all([eval_run, sibling_eval_run])
        session.flush()
        eval_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="image", name="eval.png")
        sibling_eval_asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind="image", name="eval-sibling.png")
        session.add_all([eval_asset, sibling_eval_asset])
        session.flush()
        eval_output = models.EvalOutput(eval_run_id=eval_run.id, prompt_id=prompt.id, asset_id=eval_asset.id, seed=1)
        sibling_eval_output = models.EvalOutput(eval_run_id=sibling_eval_run.id, prompt_id=prompt.id, asset_id=sibling_eval_asset.id, seed=2)
        session.add_all([eval_output, sibling_eval_output])
        session.flush()
        grid = models.GridDefinition(
            project_id=project.id, name="Guidance", eval_definition_id=definition.id,
            x_axis={"field": "guidance", "values": [3]}, y_axis={"field": "seed", "values": [1, 2]},
        )
        session.add(grid)
        session.flush()
        cell = models.GridCell(grid_definition_id=grid.id, eval_output_id=eval_output.id, ordinal=0, x_index=0, y_index=0)
        sibling_cell = models.GridCell(grid_definition_id=grid.id, eval_output_id=sibling_eval_output.id, ordinal=1, x_index=0, y_index=1)
        session.add_all([cell, sibling_cell])
        session.flush()

        session.add_all([
            models.Review(workspace_id=workspace.id, subject_type="dataset_version", subject_id=dataset_version.id, profile_id=profile.id, decision="approved"),
            models.Review(workspace_id=workspace.id, subject_type="model_version", subject_id=model_version.id, profile_id=profile.id, rating=5),
            models.Review(workspace_id=workspace.id, subject_type="eval_output", subject_id=eval_output.id, profile_id=profile.id, rating=4),
            models.Comment(workspace_id=workspace.id, subject_type="training_run", subject_id=run.id, profile_id=profile.id, body="Stable run"),
            models.Comment(workspace_id=workspace.id, subject_type="eval_run", subject_id=sibling_eval_run.id, profile_id=profile.id, body="Sibling only"),
        ])
        session.commit()
        return sessions, {
            "project": project, "other_project": other_project,
            "dataset": dataset, "dataset_version": dataset_version,
            "sibling_dataset_version": sibling_dataset_version,
            "dataset_item": dataset_item, "dataset_asset": dataset_asset,
            "run": run, "stage": stage,
            "checkpoint": checkpoint, "sample": sample, "model": model,
            "model_version": model_version, "prompt_set": prompt_set, "prompt": prompt,
            "definition": definition, "eval_run": eval_run, "sibling_eval_run": sibling_eval_run,
            "eval_output": eval_output, "sibling_eval_output": sibling_eval_output,
            "grid": grid, "cell": cell, "sibling_cell": sibling_cell,
        }


def test_project_export_manifest_contains_complete_handoff_graph(tmp_path):
    sessions, graph = _graph(tmp_path)
    handler = ExportPackageHandler(sessions, tmp_path / "exports", (tmp_path,))
    with sessions() as session:
        manifest, assets = handler._collect(session, {"project_id": graph["project"].id, "include_reviews": True})

    assert manifest["format"] == "titles-dam-export"
    assert manifest["version"] == 2
    assert _ids(manifest["projects"]) == {graph["project"].id}
    assert _ids(manifest["dataset_versions"]) == {graph["dataset_version"].id, graph["sibling_dataset_version"].id}
    assert _ids(manifest["training_runs"]) == {graph["run"].id}
    assert _ids(manifest["training_stages"]) == {graph["stage"].id}
    assert _ids(manifest["checkpoints"]) == {graph["checkpoint"].id}
    assert _ids(manifest["samples"]) == {graph["sample"].id}
    assert _ids(manifest["model_versions"]) == {graph["model_version"].id}
    assert _ids(manifest["prompts"]) == {graph["prompt"].id}
    assert _ids(manifest["eval_runs"]) == {graph["eval_run"].id, graph["sibling_eval_run"].id}
    assert _ids(manifest["eval_outputs"]) == {graph["eval_output"].id, graph["sibling_eval_output"].id}
    assert _ids(manifest["grid_cells"]) == {graph["cell"].id, graph["sibling_cell"].id}
    assert {row["subject_type"] for row in manifest["reviews"]} == {"dataset_version", "model_version", "eval_output"}
    assert {row["subject_type"] for row in manifest["comments"]} == {"training_run", "eval_run"}
    assert _ids(assets) == _ids(manifest["assets"])
    json.dumps(manifest, default=_json_default, sort_keys=True)


def test_scoped_exports_do_not_expand_to_project_or_sibling_records(tmp_path):
    sessions, graph = _graph(tmp_path)
    handler = ExportPackageHandler(sessions, tmp_path / "exports", (tmp_path,))
    with sessions() as session:
        asset_manifest, _ = handler._collect(session, {"asset_ids": [graph["dataset_asset"].id]})
        dataset_manifest, _ = handler._collect(session, {"dataset_version_ids": [graph["dataset_version"].id]})
        model_manifest, _ = handler._collect(session, {"model_version_ids": [graph["model_version"].id]})
        eval_manifest, _ = handler._collect(session, {"eval_run_ids": [graph["eval_run"].id]})

    assert _ids(asset_manifest["projects"]) == {graph["project"].id}
    assert _ids(asset_manifest["assets"]) == {graph["dataset_asset"].id}
    assert asset_manifest["datasets"] == []

    assert _ids(dataset_manifest["projects"]) == {graph["project"].id}
    assert _ids(dataset_manifest["dataset_versions"]) == {graph["dataset_version"].id}
    assert _ids(dataset_manifest["dataset_items"]) == {graph["dataset_item"].id}
    assert dataset_manifest["training_runs"] == []
    assert dataset_manifest["models"] == []

    assert _ids(model_manifest["model_versions"]) == {graph["model_version"].id}
    assert _ids(model_manifest["training_runs"]) == {graph["run"].id}
    assert _ids(model_manifest["dataset_versions"]) == {graph["dataset_version"].id}
    assert model_manifest["eval_runs"] == []
    assert model_manifest["grid_cells"] == []

    assert _ids(eval_manifest["eval_runs"]) == {graph["eval_run"].id}
    assert _ids(eval_manifest["eval_outputs"]) == {graph["eval_output"].id}
    assert _ids(eval_manifest["grid_cells"]) == {graph["cell"].id}
    assert graph["sibling_eval_run"].id not in _ids(eval_manifest["eval_runs"])
    assert {row["subject_type"] for row in eval_manifest["reviews"]} == {"dataset_version", "model_version", "eval_output"}
    assert {row["subject_type"] for row in eval_manifest["comments"]} == {"training_run"}

def test_export_includes_verified_remote_only_asset(tmp_path, monkeypatch):
    sessions, graph = _graph(tmp_path)
    content = b"remote-image"
    digest = hashlib.sha256(content).hexdigest()
    with sessions.begin() as session:
        asset = session.get(models.Asset, graph["dataset_asset"].id)
        asset.sha256 = digest
        source = models.ImportSource(
            workspace_id=asset.workspace_id,
            name="Images",
            bucket="bucket",
            allowed_prefixes=["library/"],
            credential_env_prefix="IMAGES",
        )
        session.add(source)
        session.flush()
        session.add(models.AssetLocation(
            asset_id=asset.id,
            workspace_id=asset.workspace_id,
            provider="s3",
            uri="s3://bucket/library/image.png",
            bucket="bucket",
            object_key="library/image.png",
            source_id=source.id,
            verified_size=len(content),
            verified_sha256=digest,
            verification_state="available",
            hydration_state="remote",
        ))

    class FakeClient:
        def get_object(self, **request):
            assert request["Bucket"] == "bucket"
            assert request["Key"] == "library/image.png"
            return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    monkeypatch.setattr(export_jobs, "create_source_s3_client", lambda settings: FakeClient())
    handler = ExportPackageHandler(sessions, tmp_path / "exports", (tmp_path,))
    with sessions() as session, zipfile.ZipFile(tmp_path / "remote.zip", "w") as archive:
        asset = next(handler._iter_assets(session, {graph["dataset_asset"].id}))
        assert handler._write_remote_file(
            archive,
            session,
            asset,
            f"files/{asset['id']}/image.png",
            type("Context", (), {"cancellation_requested": lambda self: False})(),
        )
    with zipfile.ZipFile(tmp_path / "remote.zip") as archive:
        assert archive.read(f"files/{graph['dataset_asset'].id}/image.png") == content


def test_export_rejects_asset_without_usable_location(tmp_path):
    sessions, graph = _graph(tmp_path)
    handler = ExportPackageHandler(sessions, tmp_path / "exports", (tmp_path,))
    with sessions() as session, zipfile.ZipFile(tmp_path / "missing.zip", "w") as archive:
        asset = next(handler._iter_assets(session, {graph["dataset_asset"].id}))
        with pytest.raises(LookupError, match="verified S3 location"):
            handler._write_remote_file(
                archive,
                session,
                asset,
                "files/missing/image.png",
                type("Context", (), {"cancellation_requested": lambda self: False})(),
            )
