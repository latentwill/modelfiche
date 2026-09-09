import importlib

import pytest


def admission_contract():
    return importlib.import_module("titles_api.storage.fal_admissions")


def test_prompt_batch_materializes_stable_subject_keys_and_ordinals():
    admission_id = "3d228c79-85a6-4a0b-9478-a960f7f33cd7"
    subjects = admission_contract().build_eval_subjects(
        admission_id=admission_id,
        mode="prompt_batch",
        prompts=[
            {"prompt_id": "prompt-a", "prompt": "one"},
            {"prompt_id": "prompt-b", "prompt": "two"},
        ],
        input_digest="f" * 64,
        expected_output_count=3,
    )

    assert [subject.ordinal for subject in subjects] == [0, 1]
    assert [subject.stable_key for subject in subjects] == [
        f"{admission_id}:prompt:prompt-a:{'f' * 64}",
        f"{admission_id}:prompt:prompt-b:{'f' * 64}",
    ]
    assert [subject.admitted_ordinals for subject in subjects] == [(0, 1, 2), (0, 1, 2)]
    assert all(subject.expected_output_count == 3 for subject in subjects)


def test_grid_materializes_one_artifact_subject_per_frozen_cell():
    admission_id = "3d228c79-85a6-4a0b-9478-a960f7f33cd7"
    subjects = admission_contract().build_grid_subjects(
        admission_id=admission_id,
        grid_definition_id="e7d5c339-1f48-4e1d-b4f4-3a1c292ee2c2",
        cells=[
            {
                "grid_cell_id": "3d93424d-e057-4cd0-897f-5f27bfd3993d",
                "x_index": 2,
                "y_index": 4,
                "axis_digest": "a" * 64,
            }
        ],
    )

    assert subjects[0].kind == "grid_cell"
    assert subjects[0].stable_key == (
        f"{admission_id}:grid_cell:e7d5c339-1f48-4e1d-b4f4-3a1c292ee2c2:2:4:{'a' * 64}"
    )
    assert subjects[0].expected_output_count == 1
    assert subjects[0].admitted_ordinals == (0,)


def test_single_admission_requires_exactly_one_prompt():
    contract = admission_contract()
    with pytest.raises(contract.FalAdmissionValidationError, match="single eval requires exactly one prompt"):
        contract.build_eval_subjects(
            admission_id="3d228c79-85a6-4a0b-9478-a960f7f33cd7",
            mode="single",
            prompts=[],
            input_digest="f" * 64,
            expected_output_count=1,
        )


def test_locally_verified_admission_requires_exact_billing_acknowledgement():
    contract = admission_contract()
    contract.validate_billing_acknowledgement("locally_verified", contract.BILLING_ACKNOWLEDGEMENT)

    with pytest.raises(contract.FalAdmissionValidationError, match="exact billing acknowledgement"):
        contract.validate_billing_acknowledgement("locally_verified", None)

    with pytest.raises(contract.FalAdmissionValidationError, match="must be null"):
        contract.validate_billing_acknowledgement("provider_verified", contract.BILLING_ACKNOWLEDGEMENT)
