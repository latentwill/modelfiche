from __future__ import annotations
from datetime import timedelta

from pydantic import ValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from titles_api import models
from titles_api.database import Base, build_engine
from titles_api.storage.repository import (
    MigrationPreviewConflict,
    PolicyConflict,
    PublicationCommitError,
    StorageRepository,
    StorageRepositoryError,
)

from titles_api.storage.contracts import (
    AssetDeliveryRequestV1,
    AlternateDeliveryRequestV1,
    parse_migration_action,
)
from titles_api.storage.identity import SourceIdentityError, normalize_source_identity
from titles_api.storage.redaction import redact_for_persistence


def test_source_identity_is_canonical_and_fingerprint_is_order_independent():
    first = normalize_source_identity(
        endpoint_url="HTTPS://S3.Example.test:8443",
        bucket="Titles",
        region="US-EAST-1",
        addressing_style="PATH",
        credential_env_prefix="titles_s3",
        allowed_prefixes=["project//images", "/project/images/"],
        managed_prefix="project/images/managed",
    )
    second = normalize_source_identity(
        endpoint_url="https://s3.example.test:8443",
        bucket="Titles",
        region="us-east-1",
        addressing_style="path",
        credential_env_prefix="TITLES_S3",
        allowed_prefixes=["project/images/"],
        managed_prefix="project/images/managed/",
    )

    assert first.endpoint == "https://s3.example.test:8443"
    assert first.allowed_prefixes == ("project/images/",)
    assert first.managed_prefix == "project/images/managed/"
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.startswith("source-v1:")


def test_source_identity_rejects_managed_prefix_outside_allowed_prefixes():
    with pytest.raises(SourceIdentityError, match="contained"):
        normalize_source_identity(
            endpoint_url="https://s3.example.test",
            bucket="titles",
            region="us-east-1",
            addressing_style="path",
            credential_env_prefix="TITLES",
            allowed_prefixes=["read-only/"],
            managed_prefix="managed/",
        )


def test_redactor_strips_signed_url_and_secret_values_recursively():
    result = redact_for_persistence(
        {
            "download_url": "https://bucket.example/object?X-Amz-Signature=top-secret#fragment",
            "authorization": "Bearer top-secret",
            "nested": {"session_token": "top-secret", "safe": "retained"},
        }
    )

    assert result == {
        "download_url": "https://bucket.example/object",
        "authorization": "[redacted]",
        "nested": {"session_token": "[redacted]", "safe": "retained"},
    }


def test_delivery_and_migration_contracts_accept_only_closed_variants():
    request = AssetDeliveryRequestV1.model_validate(
        {
            "asset_revision_id": "6ff0c0b8-0a8e-4dcd-87fd-8c7279a16457",
            "variant": {"kind": "thumbnail", "max_pixels": 512},
        }
    )
    alternate = AlternateDeliveryRequestV1.model_validate(
        {
            "alternate_token": "alternate",
            "diagnostic_token": "diagnostic",
            "asset_revision_id": request.asset_revision_id,
            "variant": request.variant.model_dump(),
        }
    )

    assert alternate.variant.kind == "thumbnail"
    assert parse_migration_action({"action": "cancel", "expected_version": 3}).action == "cancel"
    with pytest.raises(ValidationError):
        AssetDeliveryRequestV1.model_validate(
            {
                "asset_revision_id": request.asset_revision_id,
                "variant": {"kind": "thumbnail", "max_pixels": 768},
            }
        )
    with pytest.raises(ValidationError):
        parse_migration_action({"action": "unknown", "expected_version": 3})


def test_workspace_and_project_ids_are_available_before_flush(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        project = models.Project(workspace_id=workspace.id, title="Project")

        assert workspace.id is not None
        assert project.id is not None
        assert project.workspace_id == workspace.id

        session.add_all([workspace, project])
        session.flush()

        persisted_project = session.scalar(select(models.Project).where(models.Project.id == project.id))
        assert persisted_project is not None
        assert persisted_project.workspace_id == workspace.id

def test_repository_commits_only_matching_publication_receipts(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    digest = "a" * 64

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        session.add(workspace)
        session.flush()
        repository = StorageRepository(session, workspace.id)
        root = repository.create_local_root(
            kind="asset",
            canonical_path="/private/assets",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        asset = models.Asset(
            workspace_id=workspace.id,
            kind=models.AssetKind.image,
            name="generated.webp",
            provenance_kind="generated",
        )
        session.add(asset)
        session.flush()
        transfer = repository.begin_transfer(
            asset_id=asset.id,
            destination_provider="local",
            destination_root_id=root.id,
            expected_sha256=digest,
            expected_size=7,
        )
        attempt = repository.begin_publication_attempt(
            transfer_id=transfer.id,
            final_locator=f"attempts/{transfer.id}/generated.webp",
        )
        assert attempt.publication_fence == transfer.commit_fence
        repository.record_publication_receipt(
            attempt_id=attempt.id,
            provider_locator=f"attempts/{transfer.id}/generated.webp",
            sha256=digest,
            size=7,
            commit_fence=transfer.commit_fence,
        )
        location = repository.commit_publication(
            transfer_id=transfer.id,
            attempt_id=attempt.id,
            asset_id=asset.id,
            local_root_id=root.id,
            relative_path=f"attempts/{transfer.id}/generated.webp",
        )
        session.flush()

        assert location.verified_sha256 == digest
        assert asset.content_blob_id is not None
        assert asset.preferred_location_id == location.id
        assert transfer.state == "committed"


def test_repository_rejects_stale_publication_fence(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    digest = "b" * 64

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        session.add(workspace)
        session.flush()
        repository = StorageRepository(session, workspace.id)
        root = repository.create_local_root(
            kind="asset",
            canonical_path="/private/assets",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        asset = models.Asset(
            workspace_id=workspace.id,
            kind=models.AssetKind.image,
            name="generated.webp",
            provenance_kind="generated",
        )
        session.add(asset)
        session.flush()
        transfer = repository.begin_transfer(
            asset_id=asset.id,
            destination_provider="local",
            destination_root_id=root.id,
            expected_sha256=digest,
            expected_size=7,
        )
        attempt = repository.begin_publication_attempt(
            transfer_id=transfer.id,
            final_locator=f"attempts/{transfer.id}/generated.webp",
        )
        repository.record_publication_receipt(
            attempt_id=attempt.id,
            provider_locator=f"attempts/{transfer.id}/generated.webp",
            sha256=digest,
            size=7,
            commit_fence=transfer.commit_fence,
        )
        transfer.commit_fence = "replacement-fence"

        with pytest.raises(PublicationCommitError, match="fence"):
            repository.commit_publication(
                transfer_id=transfer.id,
                attempt_id=attempt.id,
                asset_id=asset.id,
                local_root_id=root.id,
                relative_path=f"attempts/{transfer.id}/generated.webp",
            )


def test_repository_uses_policy_versions_and_reclaim_pending_reservations(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        session.add(workspace)
        session.flush()
        repository = StorageRepository(session, workspace.id)
        policy = repository.reconcile_policy(
            expected_version=0,
            desired_provider="local",
            resolved_provider="local",
            selection_origin="operator",
            write_source_id=None,
            fal_local_fallback=True,
        )
        assert policy.version == 1
        with pytest.raises(PolicyConflict):
            repository.reconcile_policy(
                expected_version=0,
                desired_provider="local",
                resolved_provider="local",
                selection_origin="operator",
                write_source_id=None,
                fal_local_fallback=True,
            )

        root = repository.create_local_root(
            kind="asset",
            canonical_path="/private/assets",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        asset = models.Asset(workspace_id=workspace.id, kind=models.AssetKind.image, name="pending.webp")
        session.add(asset)
        session.flush()
        transfer = repository.begin_transfer(
            asset_id=asset.id,
            destination_provider="local",
            destination_root_id=root.id,
            expected_sha256="c" * 64,
            expected_size=9,
        )
        reservation = repository.reserve_capacity(
            transfer_id=transfer.id,
            root_id=root.id,
            kind="permanent",
            reserved_bytes=9,
        )
        repository.mark_transfer_reclaim_pending(transfer.id)

        assert reservation.state == "reclaim_pending"


def test_migration_preview_freezes_ordered_items_and_consumes_by_version(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        session.add(workspace)
        session.flush()
        repository = StorageRepository(session, workspace.id)
        root = repository.create_local_root(
            kind="asset",
            canonical_path="/private/assets",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        asset = models.Asset(workspace_id=workspace.id, kind=models.AssetKind.image, name="candidate.webp")
        session.add(asset)
        session.flush()
        preview = repository.create_migration_preview(
            scope_kind="all_images",
            project_id=None,
            destination_provider="local",
            destination_root_id=root.id,
            expires_in=timedelta(minutes=30),
            items=[
                {
                    "asset_id": asset.id,
                    "source_location_id": None,
                    "selected_revision_fingerprint": None,
                    "eligible": False,
                    "reason_code": "unverified",
                    "known_size": 0,
                }
            ],
        )
        item = session.scalar(
            select(models.StorageMigrationPreviewItem).where(
                models.StorageMigrationPreviewItem.preview_id == preview.id
            )
        )

        assert item is not None
        assert item.ordinal == 0
        assert preview.version == 0
        consumed = repository.consume_migration_preview(preview.id, expected_version=0)
        assert consumed.state == "consumed"
        assert consumed.version == 1
        with pytest.raises(MigrationPreviewConflict):
            repository.consume_migration_preview(preview.id, expected_version=0)


def test_repository_materializes_fal_subjects_before_claiming_an_intent(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        project = models.Project(workspace_id=workspace.id, title="Project")
        session.add_all([workspace, project])
        session.flush()
        repository = StorageRepository(session, workspace.id)
        root = repository.create_local_root(
            kind="spool",
            canonical_path="/private/spool",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        checkpoint_asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.model,
            name="checkpoint.safetensors",
        )
        session.add(checkpoint_asset)
        session.flush()
        run = models.TrainingRun(project_id=project.id, name="run")
        session.add(run)
        session.flush()
        checkpoint = models.Checkpoint(run_id=run.id, step=100, asset_id=checkpoint_asset.id)
        session.add(checkpoint)
        session.flush()
        revision = models.CheckpointRevision(checkpoint_id=checkpoint.id, ordinal=1, asset_id=checkpoint_asset.id)
        snapshot = models.StoragePolicySnapshot(
            workspace_id=workspace.id,
            desired_provider="local",
            resolved_provider="local",
            local_root_id=root.id,
            operation_id="fal-admission",
        )
        session.add_all([revision, snapshot])
        session.flush()

        admission = repository.create_fal_admission(
            snapshot_id=snapshot.id,
            checkpoint_revision_id=revision.id,
            endpoint_id="fal-ai/test",
            spool_root_id=root.id,
            expected_artifact_count=2,
            spool_grant_bytes=200,
            permanent_reservation_bytes=200,
            handoff_assurance="locally_verified",
            billing_acknowledged=True,
        )
        subjects = repository.create_fal_subjects(
            admission_id=admission.id,
            subjects=[
                {"subject_key": "prompt:0", "definition": {"prompt": "one"}, "expected_output_count": 1},
                {"subject_key": "prompt:1", "definition": {"prompt": "two"}, "expected_output_count": 1},
            ],
        )
        intent = repository.claim_fal_intent(subjects[0].id, generation=0, request_digest="d" * 64)

        assert admission.state == "unsubmitted"
        assert [subject.ordinal for subject in subjects] == [0, 1]
        assert intent.subject_id == subjects[0].id
        assert intent.state == "claimed"


def test_repository_fal_generation_and_receipt_fences_are_immutable(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'storage.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session.begin() as session:
        workspace = models.Workspace(name="Storage test")
        project = models.Project(workspace_id=workspace.id, title="Project")
        session.add_all([workspace, project])
        session.flush()
        repository = StorageRepository(session, workspace.id)
        root = repository.create_local_root(
            kind="spool",
            canonical_path="/private/spool",
            fingerprint="device:1",
            owner_uid=501,
            mode=0o700,
        )
        asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.model,
            name="checkpoint.safetensors",
        )
        session.add(asset)
        session.flush()
        run = models.TrainingRun(project_id=project.id, name="run")
        session.add(run)
        session.flush()
        checkpoint = models.Checkpoint(run_id=run.id, step=100, asset_id=asset.id)
        session.add(checkpoint)
        session.flush()
        revision = models.CheckpointRevision(checkpoint_id=checkpoint.id, ordinal=1, asset_id=asset.id)
        snapshot = models.StoragePolicySnapshot(
            workspace_id=workspace.id,
            desired_provider="local",
            resolved_provider="local",
            local_root_id=root.id,
            operation_id="fal-admission",
        )
        session.add_all([revision, snapshot])
        session.flush()

        admission = repository.create_fal_admission(
            snapshot_id=snapshot.id,
            checkpoint_revision_id=revision.id,
            endpoint_id="fal-ai/test",
            spool_root_id=root.id,
            expected_artifact_count=1,
            spool_grant_bytes=1,
            permanent_reservation_bytes=1,
            handoff_assurance="locally_verified",
            billing_acknowledged=True,
        )
        subject = repository.create_fal_subjects(
            admission_id=admission.id,
            subjects=[
                {"subject_key": "prompt:0", "definition": {"prompt": "one"}, "expected_output_count": 1},
            ],
        )[0]
        intent = repository.claim_fal_intent(subject.id, generation=0, request_digest="d" * 64)

        with pytest.raises(StorageRepositoryError, match="generation is stale"):
            repository.claim_fal_intent(subject.id, generation=1, request_digest="d" * 64)

        repository.create_fal_artifact_receipt(
            subject_id=subject.id,
            intent_id=intent.id,
            ordinal=0,
            artifact_identity={"provider_artifact_id": "provider-a"},
            metadata_digest="e" * 64,
            canonical_list_digest="f" * 64,
            stage_fence="stage-1",
        )
        with pytest.raises(StorageRepositoryError, match="immutable"):
            repository.create_fal_artifact_receipt(
                subject_id=subject.id,
                intent_id=intent.id,
                ordinal=0,
                artifact_identity={"provider_artifact_id": "provider-b"},
                metadata_digest="e" * 64,
                canonical_list_digest="f" * 64,
                stage_fence="stage-1",
            )
