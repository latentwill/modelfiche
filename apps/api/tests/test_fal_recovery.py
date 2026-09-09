import importlib

import pytest


def admission_contract():
    return importlib.import_module("titles_api.storage.fal_admissions")


def test_resume_same_request_keeps_selected_subject_generation():
    contract = admission_contract()
    action = contract.plan_recovery_action(
        action="resume_same_request",
        subjects=[{"id": "subject-a", "generation": 2, "state": "retryable"}],
        selected_subject_ids=["subject-a"],
        retry_requests=[],
        billing_acknowledgement=None,
    )

    assert action.kind == "resume_same_request"
    assert action.subject_generations == {"subject-a": 2}
    assert action.billable is False


def test_billable_recovery_requires_current_terminal_generation_and_acknowledgement():
    contract = admission_contract()
    action = contract.plan_recovery_action(
        action="retry_failed_subjects",
        subjects=[{"id": "subject-a", "generation": 2, "state": "failed"}],
        selected_subject_ids=[],
        retry_requests=[{"subject_id": "subject-a", "expected_terminal_generation": 2}],
        billing_acknowledgement=contract.BILLING_ACKNOWLEDGEMENT,
    )

    assert action.subject_generations == {"subject-a": 3}
    assert action.billable is True

    with pytest.raises(contract.FalAdmissionValidationError, match="stale terminal generation"):
        contract.plan_recovery_action(
            action="retry_failed_subjects",
            subjects=[{"id": "subject-a", "generation": 2, "state": "failed"}],
            selected_subject_ids=[],
            retry_requests=[{"subject_id": "subject-a", "expected_terminal_generation": 1}],
            billing_acknowledgement=contract.BILLING_ACKNOWLEDGEMENT,
        )
