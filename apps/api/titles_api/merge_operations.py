from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, schemas


def canonical_recipe(recipe: schemas.MergeRecipe | dict[str, Any]) -> dict[str, Any]:
    parsed = (
        recipe
        if isinstance(recipe, schemas.MergeRecipe)
        else schemas.MergeRecipe.model_validate(recipe)
    )
    return parsed.model_dump(mode="json", by_alias=True, exclude_none=True)


def recipe_digest(recipe: schemas.MergeRecipe | dict[str, Any]) -> str:
    payload = canonical_recipe(recipe)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def compact_notation(recipe: schemas.MergeRecipe | dict[str, Any]) -> str:
    parsed = (
        recipe
        if isinstance(recipe, schemas.MergeRecipe)
        else schemas.MergeRecipe.model_validate(recipe)
    )
    if parsed.schema_version == "modelfiche.checkpoint-merge/v1":
        operator = {
            "linear_weighted_sum": "linear-weighted-sum",
            "layer_weighted_sum": "layer-weighted-sum",
            "rank_concat": "rank-concat",
        }[parsed.resolved_operator]
        scopes = sorted(
            {scope for item in parsed.inputs for scope in item.weights},
            key=lambda value: (value != "global", value != "text_fusion", value),
        )
        clauses = []
        for scope in scopes:
            contributions = " + ".join(
                f"{item.alias}{f'@{item.step}' if item.step is not None else ''} {item.weights.get(scope, 0.0):.2f}"
                for item in parsed.inputs
                if scope in item.weights
            )
            clauses.append(f"{scope.replace('_', '-')}: {contributions}")
        return f"{operator}[r{parsed.output.rank}] {{ {'; '.join(clauses)} }} → {parsed.name}"
    method = parsed.method
    assert method is not None
    labels = {
        item.alias: f"{item.alias}{f'@{item.step}' if item.step is not None else ''}"
        for item in parsed.inputs
    }
    anchor_label = labels[method.anchor]
    parameters = method.parameters
    if method.kind == "slerp":
        if method.donor is not None:
            donor_aliases = [method.donor]
        elif method.donors is not None:
            donor_aliases = method.donors
        else:
            donor_aliases = [
                item.alias for item in parsed.inputs if item.alias != method.anchor
            ]
        aliases = [method.anchor, *donor_aliases]
        if "donor_weight" in parameters:
            donor_weight = float(parameters["donor_weight"])
            clause = f"{anchor_label} {1.0 - donor_weight:.2f} ↝ {labels[donor_aliases[0]]} {donor_weight:.2f}"
        else:

            def render_weights(spec: Any) -> str:
                if spec == "auto":
                    return "auto(balanced-energy)"
                return " + ".join(
                    f"{labels[alias]} {float(spec[alias]):.2f}" for alias in aliases
                )

            clauses = [
                f"anchor={anchor_label}",
                f"global: {render_weights(parameters['weights'])}",
            ]
            if "text_fusion_weights" in parameters:
                clauses.append(
                    f"text-fusion: {render_weights(parameters['text_fusion_weights'])}"
                )
            if "transformer_weights" in parameters:
                clauses.append(
                    f"transformer: {render_weights(parameters['transformer_weights'])}"
                )
            clauses.extend(
                f"b{int(item['start']):02d}-{int(item['end']):02d}: {render_weights(item['weights'])}"
                for item in parameters.get("transformer_blocks", [])
            )
            clause = "; ".join(clauses)
    else:
        assert method.donor is not None
        donor_label = labels[method.donor]
        if method.kind in {"weighted_sum", "norm_balanced"}:
            clause = f"{anchor_label} {float(parameters['anchor_weight']):.2f} + {donor_label} {float(parameters['donor_weight']):.2f}"
        elif method.kind == "delta_add":
            ranges = ",".join(
                f"b{int(item['start']):02d}-{int(item['end']):02d}=+{float(item['donor']):.2f}"
                for item in parameters["transformer_blocks"]
            )
            clause = f"anchor={anchor_label}; donor={donor_label}; tf=+{float(parameters['text_fusion_donor']):.2f}; {ranges}"
        elif method.kind == "cosine_gated":
            clause = (
                f"anchor={anchor_label}; donor={donor_label}; "
                f"donor={float(parameters['donor_min']):.2f}..{float(parameters['donor_max']):.2f}; "
                f"pct={float(parameters.get('lower_percentile', 5)):g}..{float(parameters.get('upper_percentile', 95)):g}"
            )
        else:
            clause = f"anchor={anchor_label}; donor={donor_label}; novelty<={float(parameters['novelty_cap']):.2f}"
    return f"{method.kind.replace('_', '-')}[r{parsed.output.rank}] {{ {clause} }} → {parsed.name}"


def merge_projection(
    db: Session,
    operation: models.MergeOperation,
    *,
    output_directory: str | None = None,
    cache: Any | None = None,
) -> dict[str, Any]:
    if cache is not None:
        inputs = cache.merge_inputs.get(operation.id, [])
    else:
        inputs = db.scalars(
            select(models.MergeInput)
            .where(models.MergeInput.merge_operation_id == operation.id)
            .order_by(models.MergeInput.position)
        ).all()
    input_rows = []
    for merge_input in inputs:
        if cache is not None:
            weights = cache.merge_input_weights.get(merge_input.id, [])
            revision = cache.revisions.get(merge_input.checkpoint_revision_id)
            checkpoint = cache.checkpoints.get(revision.checkpoint_id) if revision else None
            asset = cache.assets.get(revision.asset_id) if revision else None
        else:
            weights = db.scalars(
                select(models.MergeInputWeight)
                .where(models.MergeInputWeight.merge_input_id == merge_input.id)
                .order_by(models.MergeInputWeight.scope)
            ).all()
            revision = db.get(models.CheckpointRevision, merge_input.checkpoint_revision_id)
            checkpoint = db.get(models.Checkpoint, revision.checkpoint_id) if revision else None
            asset = db.get(models.Asset, revision.asset_id) if revision else None
        location = None
        if revision and revision.source_location_id:
            if cache is not None:
                location = cache.locations_by_id.get(revision.source_location_id)
            else:
                location = db.get(models.AssetLocation, revision.source_location_id)
        input_rows.append(
            {
                "id": merge_input.id,
                "alias": merge_input.alias,
                "role": merge_input.role,
                "position": merge_input.position,
                "source_rank": merge_input.source_rank,
                "checkpoint_revision_id": merge_input.checkpoint_revision_id,
                "checkpoint_id": checkpoint.id if checkpoint else None,
                "checkpoint_step": checkpoint.step if checkpoint else None,
                "asset_id": asset.id if asset else None,
                "sha256": asset.sha256 if asset else None,
                "source_sha256": asset.sha256 if asset else None,
                "step": checkpoint.step if checkpoint else None,
                "local_path": location.uri.removeprefix("file://")
                if location
                and location.provider == "local"
                and location.verification_state in {"available", "verified"}
                else None,
                "weights": {weight.scope: weight.weight for weight in weights},
            }
        )
    if cache is not None:
        output_revision = (
            cache.revisions.get(operation.output_checkpoint_revision_id)
            if operation.output_checkpoint_revision_id
            else None
        )
        output_checkpoint = (
            cache.checkpoints.get(output_revision.checkpoint_id)
            if output_revision
            else None
        )
        output_asset = (
            cache.assets.get(output_revision.asset_id) if output_revision else None
        )
    else:
        output_revision = (
            db.get(models.CheckpointRevision, operation.output_checkpoint_revision_id)
            if operation.output_checkpoint_revision_id
            else None
        )
        output_checkpoint = (
            db.get(models.Checkpoint, output_revision.checkpoint_id)
            if output_revision
            else None
        )
        output_asset = (
            db.get(models.Asset, output_revision.asset_id) if output_revision else None
        )
    if cache is not None:
        model_version = (
            cache.revision_version.get(operation.output_checkpoint_revision_id)
            if operation.output_checkpoint_revision_id
            else None
        )
    else:
        model_version = (
            db.scalar(
                select(models.ModelVersion).where(
                    models.ModelVersion.checkpoint_revision_id
                    == operation.output_checkpoint_revision_id
                )
            )
            if operation.output_checkpoint_revision_id
            else None
        )
    result = {
        "id": operation.id,
        "workspace_id": operation.workspace_id,
        "project_id": operation.project_id,
        "run_id": operation.run_id,
        "schema_version": operation.schema_version,
        "operator": operation.operator,
        "base_model": operation.base_model,
        "status": operation.status,
        "recipe": operation.recipe,
        "recipe_digest": operation.recipe_digest,
        "compact_notation": operation.compact_notation,
        "notation": operation.compact_notation,
        "output_rank": operation.output_rank,
        "dtype": operation.dtype,
        "output_checkpoint_revision_id": operation.output_checkpoint_revision_id,
        "checkpoint_id": output_checkpoint.id if output_checkpoint else None,
        "asset_id": output_asset.id if output_asset else None,
        "output_sha256": output_asset.sha256 if output_asset else None,
        "model_version_id": model_version.id if model_version else None,
        "error_code": operation.error_code,
        "error_message": operation.error_message,
        "failure_phase": operation.failure_phase,
        "last_heartbeat_at": operation.last_heartbeat_at,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "inputs": input_rows,
        "output": {
            "rank": operation.output_rank,
            "dtype": operation.dtype,
            "checkpoint_revision_id": operation.output_checkpoint_revision_id,
            "checkpoint_id": output_checkpoint.id if output_checkpoint else None,
            "asset_id": output_asset.id if output_asset else None,
            "sha256": output_asset.sha256 if output_asset else None,
            "model_version_id": model_version.id if model_version else None,
            "model_version_name": model_version.name if model_version else None,
        },
    }
    if output_directory is not None:
        result["output_directory"] = output_directory
    return result
