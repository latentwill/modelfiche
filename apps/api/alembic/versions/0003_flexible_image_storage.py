"""Authoritative flexible Local/S3 storage compatibility schema.

This migration is intentionally static.  It does not inspect SQLAlchemy metadata or
condition its shape on the database it happens to be run against; the baseline
classifier is the only admission path for installed databases.
"""

from alembic import op


revision = "0003_flexible_image_storage"
down_revision = "0002_generic_s3_sources"
branch_labels = None
depends_on = None


_ALTERS = (
    "ALTER TABLE import_sources ADD COLUMN identity_fingerprint VARCHAR(80)",
    "ALTER TABLE import_sources ADD COLUMN managed_prefix VARCHAR(1024)",
    "ALTER TABLE assets ADD COLUMN legacy_asset_id VARCHAR(36)",
    "ALTER TABLE assets ADD COLUMN content_blob_id VARCHAR(36) REFERENCES content_blobs(id)",
    "ALTER TABLE assets ADD COLUMN preferred_location_id VARCHAR(36) REFERENCES asset_locations(id)",
    "ALTER TABLE assets ADD COLUMN origin_location_id VARCHAR(36) REFERENCES asset_locations(id)",
    "ALTER TABLE assets ADD COLUMN provenance_kind VARCHAR(16) NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE assets ADD COLUMN generated_artifact_receipt_id VARCHAR(36) REFERENCES fal_artifact_receipts(id)",
    "ALTER TABLE asset_locations ADD COLUMN workspace_id VARCHAR(36) REFERENCES workspaces(id)",
    "ALTER TABLE asset_locations ADD COLUMN source_id VARCHAR(36) REFERENCES import_sources(id)",
    "ALTER TABLE asset_locations ADD COLUMN source_revision_fingerprint VARCHAR(255)",
    "ALTER TABLE asset_locations ADD COLUMN version_id VARCHAR(1024)",
    "ALTER TABLE asset_locations ADD COLUMN local_root_id VARCHAR(36) REFERENCES local_roots(id)",
    "ALTER TABLE asset_locations ADD COLUMN relative_path VARCHAR(2048)",
    "ALTER TABLE asset_locations ADD COLUMN verified_size INTEGER",
    "ALTER TABLE asset_locations ADD COLUMN verified_sha256 VARCHAR(64)",
    "ALTER TABLE asset_locations ADD COLUMN verification_state VARCHAR(32) NOT NULL DEFAULT 'unverified'",
    "ALTER TABLE asset_locations ADD COLUMN last_verified_at DATETIME",
    "ALTER TABLE asset_locations ADD COLUMN repair_attribution JSON",
    "ALTER TABLE training_runs ADD COLUMN origin_source_id VARCHAR(36) REFERENCES import_sources(id)",
    "ALTER TABLE training_runs ADD COLUMN origin_source_fingerprint VARCHAR(80)",
    "ALTER TABLE checkpoints ADD COLUMN current_revision_id VARCHAR(36) REFERENCES checkpoint_revisions(id)",
    "ALTER TABLE model_versions ADD COLUMN checkpoint_revision_id VARCHAR(36) REFERENCES checkpoint_revisions(id)",
    "ALTER TABLE eval_runs ADD COLUMN checkpoint_revision_id VARCHAR(36) REFERENCES checkpoint_revisions(id)",
    "ALTER TABLE eval_runs ADD COLUMN fal_admission_id VARCHAR(36) REFERENCES fal_admissions(id)",
    "ALTER TABLE samples ADD COLUMN supersedes_sample_id VARCHAR(36) REFERENCES samples(id)",
    "ALTER TABLE jobs ADD COLUMN storage_contract_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN storage_policy_snapshot_id VARCHAR(36) REFERENCES storage_policy_snapshots(id)",
    "ALTER TABLE jobs ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN terminal_reason_code VARCHAR(128)",
)


_TABLES = (
    """
    CREATE TABLE content_blobs (
        id VARCHAR(36) PRIMARY KEY,
        sha256 VARCHAR(64) NOT NULL UNIQUE,
        size INTEGER NOT NULL CHECK(size >= 0),
        mime_type VARCHAR(200) NOT NULL,
        verified_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE local_roots (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        kind VARCHAR(32) NOT NULL,
        canonical_path VARCHAR(2048) NOT NULL,
        fingerprint JSON NOT NULL,
        owner_uid INTEGER NOT NULL,
        mode INTEGER NOT NULL,
        availability VARCHAR(32) NOT NULL,
        retired_at DATETIME,
        created_at DATETIME NOT NULL,
        UNIQUE(workspace_id, canonical_path)
    )
    """,
    """
    CREATE TABLE storage_policies (
        workspace_id VARCHAR(36) PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 0,
        desired_provider VARCHAR(16) NOT NULL CHECK(desired_provider IN ('local', 's3')),
        resolved_provider VARCHAR(16) NOT NULL CHECK(resolved_provider IN ('local', 's3')),
        write_source_id VARCHAR(36) REFERENCES import_sources(id),
        selection_origin VARCHAR(16) NOT NULL CHECK(selection_origin IN ('automatic', 'operator')),
        fal_local_fallback BOOLEAN NOT NULL DEFAULT 1,
        blocked_reason VARCHAR(128),
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE storage_policy_snapshots (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        contract_version INTEGER NOT NULL,
        desired_provider VARCHAR(16) NOT NULL,
        resolved_provider VARCHAR(16) NOT NULL,
        origin_source_id VARCHAR(36) REFERENCES import_sources(id),
        origin_source_fingerprint VARCHAR(80),
        write_source_id VARCHAR(36) REFERENCES import_sources(id),
        write_source_fingerprint VARCHAR(80),
        managed_prefix VARCHAR(1024),
        local_root_id VARCHAR(36) REFERENCES local_roots(id),
        fal_local_fallback BOOLEAN NOT NULL,
        operation_id VARCHAR(36) NOT NULL,
        actor_profile_id VARCHAR(36) REFERENCES user_profiles(id),
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE source_capabilities (
        id VARCHAR(36) PRIMARY KEY,
        source_id VARCHAR(36) NOT NULL REFERENCES import_sources(id) ON DELETE CASCADE,
        source_fingerprint VARCHAR(80) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        boot_id VARCHAR(36) NOT NULL,
        credential_epoch VARCHAR(128) NOT NULL,
        capability VARCHAR(64) NOT NULL,
        state VARCHAR(32) NOT NULL,
        expires_at DATETIME NOT NULL,
        redacted_detail VARCHAR(512),
        created_at DATETIME NOT NULL,
        UNIQUE(source_id, capability, source_fingerprint, adapter_version, boot_id, credential_epoch)
    )
    """,
    """
    CREATE TABLE storage_transfers (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        snapshot_id VARCHAR(36) NOT NULL REFERENCES storage_policy_snapshots(id),
        asset_id VARCHAR(36) REFERENCES assets(id),
        source_location_id VARCHAR(36) REFERENCES asset_locations(id),
        destination_provider VARCHAR(16) NOT NULL CHECK(destination_provider IN ('local', 's3')),
        destination_source_id VARCHAR(36) REFERENCES import_sources(id),
        destination_root_id VARCHAR(36) REFERENCES local_roots(id),
        state VARCHAR(32) NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        lease_owner VARCHAR(128),
        lease_expires_at DATETIME,
        commit_fence VARCHAR(128) NOT NULL,
        expected_sha256 VARCHAR(64),
        expected_size INTEGER CHECK(expected_size IS NULL OR expected_size >= 0),
        migration_id VARCHAR(36) REFERENCES storage_migrations(id),
        migration_owner_epoch INTEGER,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE publication_attempts (
        id VARCHAR(36) PRIMARY KEY,
        transfer_id VARCHAR(36) NOT NULL REFERENCES storage_transfers(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL,
        nonce VARCHAR(128) NOT NULL,
        publication_fence VARCHAR(128) NOT NULL,
        final_locator VARCHAR(2048) NOT NULL,
        expected_sha256 VARCHAR(64),
        expected_size INTEGER CHECK(expected_size IS NULL OR expected_size >= 0),
        state VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        UNIQUE(transfer_id, generation)
    )
    """,
    """
    CREATE TABLE publication_receipts (
        id VARCHAR(36) PRIMARY KEY,
        attempt_id VARCHAR(36) NOT NULL UNIQUE REFERENCES publication_attempts(id) ON DELETE CASCADE,
        provider_locator VARCHAR(2048) NOT NULL,
        version_id VARCHAR(1024),
        etag VARCHAR(255),
        size INTEGER NOT NULL CHECK(size >= 0),
        sha256 VARCHAR(64) NOT NULL,
        publication_fence VARCHAR(128) NOT NULL,
        commit_fence VARCHAR(128) NOT NULL,
        verified_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE cleanup_receipts (
        id VARCHAR(36) PRIMARY KEY,
        attempt_id VARCHAR(36) NOT NULL REFERENCES publication_attempts(id) ON DELETE CASCADE,
        source_id VARCHAR(36) REFERENCES import_sources(id),
        version_id VARCHAR(1024),
        cleanup_fence VARCHAR(128) NOT NULL,
        deleted_at DATETIME NOT NULL,
        UNIQUE(attempt_id, cleanup_fence)
    )
    """,
    """
    CREATE TABLE storage_leases (
        id VARCHAR(36) PRIMARY KEY,
        subject_kind VARCHAR(64) NOT NULL,
        subject_id VARCHAR(36) NOT NULL,
        owner VARCHAR(128) NOT NULL,
        generation INTEGER NOT NULL,
        state VARCHAR(32) NOT NULL,
        expires_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(subject_kind, subject_id)
    )
    """,
    """
    CREATE TABLE storage_reservations (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        transfer_id VARCHAR(36) REFERENCES storage_transfers(id),
        root_id VARCHAR(36) REFERENCES local_roots(id),
        kind VARCHAR(64) NOT NULL,
        reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
        state VARCHAR(32) NOT NULL CHECK(state IN ('active', 'reclaim_pending', 'released')),
        generation INTEGER NOT NULL DEFAULT 0,
        lease_owner VARCHAR(128),
        lease_expires_at DATETIME,
        created_at DATETIME NOT NULL,
        released_at DATETIME,
        UNIQUE(kind, transfer_id, generation)
    )
    """,
    """
    CREATE TABLE storage_migrations (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        snapshot_id VARCHAR(36) NOT NULL REFERENCES storage_policy_snapshots(id),
        version INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(32) NOT NULL,
        scope_kind VARCHAR(32) NOT NULL,
        project_id VARCHAR(36) REFERENCES projects(id),
        destination_provider VARCHAR(16) NOT NULL CHECK(destination_provider IN ('local', 's3')),
        destination_source_id VARCHAR(36) REFERENCES import_sources(id),
        destination_root_id VARCHAR(36) REFERENCES local_roots(id),
        reason_code VARCHAR(128),
        predecessor_id VARCHAR(36) REFERENCES storage_migrations(id),
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE migration_execution_owners (
        workspace_id VARCHAR(36) PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        parent_id VARCHAR(36) NOT NULL UNIQUE REFERENCES storage_migrations(id) ON DELETE CASCADE,
        owner_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE storage_migration_items (
        id VARCHAR(36) PRIMARY KEY,
        migration_id VARCHAR(36) NOT NULL REFERENCES storage_migrations(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        asset_id VARCHAR(36) NOT NULL REFERENCES assets(id),
        content_blob_id VARCHAR(36) REFERENCES content_blobs(id),
        source_location_id VARCHAR(36) NOT NULL REFERENCES asset_locations(id),
        destination_provider VARCHAR(16) NOT NULL CHECK(destination_provider IN ('local', 's3')),
        destination_source_id VARCHAR(36) REFERENCES import_sources(id),
        destination_root_id VARCHAR(36) REFERENCES local_roots(id),
        eligibility VARCHAR(32) NOT NULL,
        reason_code VARCHAR(128),
        outcome VARCHAR(32),
        generation INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(migration_id, ordinal)
    )
    """,
    """
    CREATE TABLE storage_migration_previews (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(16) NOT NULL CHECK(state IN ('open', 'consumed', 'expired')),
        expires_at DATETIME NOT NULL,
        scope_kind VARCHAR(32) NOT NULL,
        project_id VARCHAR(36) REFERENCES projects(id),
        destination_provider VARCHAR(16) NOT NULL CHECK(destination_provider IN ('local', 's3')),
        destination_source_id VARCHAR(36) REFERENCES import_sources(id),
        destination_root_id VARCHAR(36) REFERENCES local_roots(id),
        predecessor_id VARCHAR(36) REFERENCES storage_migrations(id),
        predecessor_version INTEGER,
        expected_owner_epoch INTEGER,
        frozen_fields JSON NOT NULL,
        eligible_count INTEGER NOT NULL,
        excluded_count INTEGER NOT NULL,
        known_bytes INTEGER NOT NULL,
        unknown_bytes INTEGER NOT NULL,
        worst_case_bytes INTEGER NOT NULL,
        readiness_state VARCHAR(32) NOT NULL,
        disabled_reason VARCHAR(128),
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE storage_migration_preview_items (
        id VARCHAR(36) PRIMARY KEY,
        preview_id VARCHAR(36) NOT NULL REFERENCES storage_migration_previews(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        asset_id VARCHAR(36) NOT NULL REFERENCES assets(id),
        content_blob_id VARCHAR(36) REFERENCES content_blobs(id),
        source_location_id VARCHAR(36) REFERENCES asset_locations(id),
        selected_revision_fingerprint VARCHAR(255),
        destination_provider VARCHAR(16) NOT NULL CHECK(destination_provider IN ('local', 's3')),
        destination_source_id VARCHAR(36) REFERENCES import_sources(id),
        destination_root_id VARCHAR(36) REFERENCES local_roots(id),
        eligible BOOLEAN NOT NULL,
        reason_code VARCHAR(128),
        known_size INTEGER,
        UNIQUE(preview_id, ordinal)
    )
    """,
    """
    CREATE TABLE checkpoint_revisions (
        id VARCHAR(36) PRIMARY KEY,
        checkpoint_id VARCHAR(36) NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        asset_id VARCHAR(36) NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
        content_blob_id VARCHAR(36) REFERENCES content_blobs(id),
        source_location_id VARCHAR(36) REFERENCES asset_locations(id),
        supersedes_id VARCHAR(36) REFERENCES checkpoint_revisions(id),
        created_at DATETIME NOT NULL,
        UNIQUE(checkpoint_id, ordinal)
    )
    """,
    """
    CREATE TABLE storage_imports (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        snapshot_id VARCHAR(36) NOT NULL REFERENCES storage_policy_snapshots(id),
        source_id VARCHAR(36) NOT NULL REFERENCES import_sources(id),
        local_root_id VARCHAR(36) REFERENCES local_roots(id),
        mode VARCHAR(16) NOT NULL CHECK(mode IN ('keep', 'copy')),
        version INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(32) NOT NULL,
        reservation_id VARCHAR(36) REFERENCES storage_reservations(id),
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE storage_import_items (
        id VARCHAR(36) PRIMARY KEY,
        import_id VARCHAR(36) NOT NULL REFERENCES storage_imports(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        object_key VARCHAR(1024) NOT NULL,
        source_revision_fingerprint VARCHAR(255) NOT NULL,
        size INTEGER,
        state VARCHAR(32) NOT NULL,
        transfer_id VARCHAR(36) REFERENCES storage_transfers(id),
        outcome VARCHAR(32),
        generation INTEGER NOT NULL DEFAULT 0,
        UNIQUE(import_id, ordinal)
    )
    """,
    """
    CREATE TABLE storage_return_drafts (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        kind VARCHAR(64) NOT NULL,
        payload JSON NOT NULL,
        resource_id VARCHAR(36) NOT NULL,
        resource_version INTEGER NOT NULL,
        invoker_id VARCHAR(128) NOT NULL,
        state VARCHAR(16) NOT NULL CHECK(state IN ('open', 'consumed', 'invalidated')),
        expires_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        consumed_at DATETIME
    )
    """,
    """
    CREATE TABLE fal_admissions (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        eval_run_id VARCHAR(36) REFERENCES eval_runs(id) ON DELETE SET NULL,
        snapshot_id VARCHAR(36) NOT NULL REFERENCES storage_policy_snapshots(id) ON DELETE RESTRICT,
        checkpoint_revision_id VARCHAR(36) NOT NULL REFERENCES checkpoint_revisions(id) ON DELETE RESTRICT,
        staging_source_id VARCHAR(36) REFERENCES import_sources(id) ON DELETE RESTRICT,
        endpoint_id VARCHAR(240) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(32) NOT NULL,
        handoff_assurance VARCHAR(32) NOT NULL,
        billing_acknowledged_at DATETIME,
        expected_artifact_count INTEGER NOT NULL CHECK(expected_artifact_count >= 0),
        spool_root_id VARCHAR(36) NOT NULL REFERENCES local_roots(id) ON DELETE RESTRICT,
        spool_grant_bytes INTEGER NOT NULL CHECK(spool_grant_bytes >= 0),
        permanent_reservation_bytes INTEGER NOT NULL CHECK(permanent_reservation_bytes >= 0),
        dispatch_fence VARCHAR(128) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE fal_subjects (
        id VARCHAR(36) PRIMARY KEY,
        admission_id VARCHAR(36) NOT NULL REFERENCES fal_admissions(id) ON DELETE CASCADE,
        subject_key VARCHAR(256) NOT NULL,
        ordinal INTEGER NOT NULL,
        definition JSON NOT NULL,
        expected_output_count INTEGER NOT NULL CHECK(expected_output_count >= 0),
        generation INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(admission_id, ordinal),
        UNIQUE(admission_id, subject_key, generation)
    )
    """,
    """
    CREATE TABLE fal_submission_intents (
        id VARCHAR(36) PRIMARY KEY,
        admission_id VARCHAR(36) NOT NULL REFERENCES fal_admissions(id) ON DELETE CASCADE,
        subject_id VARCHAR(36) NOT NULL REFERENCES fal_subjects(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL,
        request_digest VARCHAR(64) NOT NULL,
        state VARCHAR(32) NOT NULL,
        provider_request_id VARCHAR(500),
        submission_fence VARCHAR(128) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(subject_id, generation)
    )
    """,
    """
    CREATE TABLE fal_artifact_receipts (
        id VARCHAR(36) PRIMARY KEY,
        subject_id VARCHAR(36) NOT NULL REFERENCES fal_subjects(id) ON DELETE CASCADE,
        intent_id VARCHAR(36) NOT NULL REFERENCES fal_submission_intents(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        artifact_identity JSON NOT NULL,
        metadata_digest VARCHAR(64) NOT NULL,
        canonical_list_digest VARCHAR(64) NOT NULL,
        state VARCHAR(32) NOT NULL,
        observed_size INTEGER,
        observed_sha256 VARCHAR(64),
        stage_fence VARCHAR(128) NOT NULL,
        transfer_id VARCHAR(36) REFERENCES storage_transfers(id) ON DELETE SET NULL,
        eval_output_id VARCHAR(36) REFERENCES eval_outputs(id) ON DELETE SET NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(subject_id, intent_id, ordinal)
    )
    """,
    """
    CREATE TABLE storage_feature_gates (
        name TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('disabled','migrating','ready','blocked')),
        version INTEGER NOT NULL DEFAULT 0,
        verification_receipt_id TEXT,
        blocked_reason TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(verification_receipt_id) REFERENCES storage_verification_receipts(id)
    )
    """,
    """
    CREATE TABLE storage_verification_receipts (
        id TEXT PRIMARY KEY,
        gate_name TEXT NOT NULL,
        parent_receipt_id TEXT,
        run_lease_id TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        evidence_payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(gate_name, parent_receipt_id, run_lease_id, evidence_digest)
    )
    """,
    """
    CREATE TABLE storage_cutover_marker (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        marker INTEGER NOT NULL,
        binding_digest TEXT NOT NULL,
        effect_epoch INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE raw_cutover_backups (
        id VARCHAR(36) PRIMARY KEY,
        database_fingerprint VARCHAR(64) NOT NULL,
        envelope_digest VARCHAR(64) NOT NULL,
        key_id VARCHAR(128) NOT NULL,
        state VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        purge_receipt JSON
    )
    """,
    """
    CREATE TABLE sanitized_cutover_backups (
        id VARCHAR(36) PRIMARY KEY,
        raw_backup_id VARCHAR(36) NOT NULL REFERENCES raw_cutover_backups(id),
        head_revision VARCHAR(64) NOT NULL,
        scrub_manifest_digest VARCHAR(64) NOT NULL,
        validator_digest VARCHAR(64) NOT NULL,
        envelope_digest VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE sanitized_backup_bindings (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        backup_id VARCHAR(36) NOT NULL UNIQUE REFERENCES sanitized_cutover_backups(id),
        header_digest VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE cutover_attempts (
        id VARCHAR(36) PRIMARY KEY,
        state VARCHAR(32) NOT NULL,
        journal_path VARCHAR(2048) NOT NULL,
        raw_backup_id VARCHAR(36) REFERENCES raw_cutover_backups(id),
        sanitized_backup_id VARCHAR(36) REFERENCES sanitized_cutover_backups(id),
        target_head VARCHAR(64) NOT NULL,
        target_marker INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        finalized_at DATETIME
    )
    """,
    """
    CREATE TABLE post_marker_restore_attempts (
        id VARCHAR(36) PRIMARY KEY,
        backup_id VARCHAR(36) NOT NULL REFERENCES sanitized_cutover_backups(id),
        restore_generation VARCHAR(64) NOT NULL UNIQUE,
        state VARCHAR(32) NOT NULL,
        journal_path VARCHAR(2048) NOT NULL,
        created_at DATETIME NOT NULL,
        completed_at DATETIME
    )
    """,
)


_INDEXES = (
    "CREATE UNIQUE INDEX uq_import_sources_identity_fingerprint ON import_sources(workspace_id, identity_fingerprint) WHERE identity_fingerprint IS NOT NULL",
    "CREATE UNIQUE INDEX uq_assets_legacy_asset_id ON assets(legacy_asset_id) WHERE legacy_asset_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_asset_locations_source_revision ON asset_locations(workspace_id, source_id, object_key, source_revision_fingerprint) WHERE source_id IS NOT NULL AND source_revision_fingerprint IS NOT NULL",
    "CREATE INDEX ix_asset_locations_blob_sha ON asset_locations(verified_sha256)",
    "CREATE INDEX ix_source_capabilities_current ON source_capabilities(source_id, capability, expires_at)",
    "CREATE UNIQUE INDEX uq_storage_migrations_active_workspace ON storage_migrations(workspace_id) WHERE state IN ('queued', 'running', 'cancel_requested')",
    "CREATE INDEX ix_storage_migration_items_state ON storage_migration_items(migration_id, outcome)",
    "CREATE UNIQUE INDEX uq_checkpoint_revisions_supersedes ON checkpoint_revisions(supersedes_id) WHERE supersedes_id IS NOT NULL",
    "CREATE INDEX ix_storage_transfers_migration ON storage_transfers(migration_id, migration_owner_epoch)",
    "CREATE INDEX ix_storage_reservations_transfer_state ON storage_reservations(transfer_id, state)",
    "CREATE INDEX ix_storage_import_items_state ON storage_import_items(import_id, state)",
    "CREATE INDEX ix_fal_submission_intents_state ON fal_submission_intents(state)",
)


_GATE_NAMES = (
    "storage_contract",
    "source_bound_io",
    "remote_delivery",
    "policy_import",
    "policy_fal",
    "storage_migration",
)


def upgrade() -> None:
    bind = op.get_bind()
    for statement in _TABLES:
        bind.exec_driver_sql(statement)
    for statement in _ALTERS:
        bind.exec_driver_sql(statement)
    for statement in _INDEXES:
        bind.exec_driver_sql(statement)
    for gate_name in _GATE_NAMES:
        bind.exec_driver_sql(
            "INSERT INTO storage_feature_gates(name,state,version,updated_at) VALUES (?, 'disabled', 0, CURRENT_TIMESTAMP)",
            (gate_name,),
        )


def downgrade() -> None:
    raise NotImplementedError("the compatibility release is a forward-only cutover")
