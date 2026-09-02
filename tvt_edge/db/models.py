"""SQLAlchemy model for the authoritative TVT management database.

The schema deliberately contains management metadata only. Camera credentials
are application-encrypted before they reach a database column; Kubernetes
Secret bodies and credential-bearing RTSP URLs have no representation here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimeMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Site(Base, IdMixin, TimeMixin):
    __tablename__ = "sites"
    site_key: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    edge_id: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(100), default="UTC", nullable=False)
    config_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class SiteConfigRevision(Base, IdMixin):
    __tablename__ = "site_config_revisions"
    __table_args__ = (UniqueConstraint("site_id", "revision"),)
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DiscoveryScope(Base, IdMixin, TimeMixin):
    __tablename__ = "discovery_scopes"
    __table_args__ = (UniqueConstraint("site_id", "interface_name", "cidr"),)
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    interface_name: Mapped[str] = mapped_column(String(64), nullable=False)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    rtsp_ports: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Camera(Base, IdMixin, TimeMixin):
    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("site_id", "camera_key"),
        CheckConstraint(
            "onboarding_state IN ('discovered','needs_credentials','validating',"
            "'online','offline','invalid','disabled','deleted')",
            name="camera_onboarding_state",
        ),
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    camera_key: Mapped[str] = mapped_column(String(63), nullable=False)
    friendly_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(200))
    firmware_version: Mapped[str | None] = mapped_column(String(200))
    identity_state: Mapped[str] = mapped_column(
        String(32), default="provisional", nullable=False
    )
    onboarding_state: Mapped[str] = mapped_column(
        String(32), default="discovered", nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CameraIdentifier(Base, IdMixin):
    __tablename__ = "camera_identifiers"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('mac','serial','onvif_endpoint_uuid','onvif_device_id')",
            name="camera_identifier_kind",
        ),
        Index(
            "uq_active_camera_identifier",
            "kind",
            "normalized_value",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(255), nullable=False)
    display_value: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="observed", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CameraRole(Base, IdMixin, TimeMixin):
    __tablename__ = "camera_roles"
    __table_args__ = (UniqueConstraint("site_id", "role_key"),)
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False
    )
    role_key: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class CameraRoleAssignment(Base, IdMixin):
    __tablename__ = "camera_role_assignments"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('entry','exit','bidirectional','unknown')",
            name="camera_role_direction",
        ),
        Index(
            "uq_active_camera_role_assignment",
            "camera_id",
            "role_id",
            "direction",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera_roles.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CameraEndpoint(Base, IdMixin, TimeMixin):
    __tablename__ = "camera_endpoints"
    __table_args__ = (
        UniqueConstraint("camera_id", "kind", "host", "port", "path"),
        CheckConstraint(
            "scheme IN ('http','https','rtsp','rtsps')", name="camera_endpoint_scheme"
        ),
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scheme: Mapped[str] = mapped_column(String(8), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), default="/", nullable=False)
    interface_name: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CameraOnvifConfig(Base):
    __tablename__ = "camera_onvif_config"
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), primary_key=True
    )
    device_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("camera_endpoints.id", ondelete="RESTRICT")
    )
    media_service_path: Mapped[str | None] = mapped_column(String(1024))
    scopes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_queried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CameraStreamProfile(Base, IdMixin, TimeMixin):
    __tablename__ = "camera_stream_profiles"
    __table_args__ = (
        UniqueConstraint("camera_id", "profile_token"),
        CheckConstraint("transport IN ('tcp','udp')", name="camera_stream_transport"),
        Index(
            "uq_selected_camera_stream",
            "camera_id",
            unique=True,
            postgresql_where=text("selected"),
            sqlite_where=text("selected = 1"),
        ),
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera_endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    profile_token: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_name: Mapped[str | None] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    transport: Mapped[str] = mapped_column(String(8), default="tcp", nullable=False)
    codec: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Numeric(8, 3))
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialKeyVersion(Base):
    __tablename__ = "credential_key_versions"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    algorithm: Mapped[str] = mapped_column(
        String(32), default="AES-256-GCM", nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CameraCredentialVersion(Base, IdMixin):
    __tablename__ = "camera_credential_versions"
    __table_args__ = (
        UniqueConstraint("camera_id", "credential_version"),
        UniqueConstraint("key_version", "nonce"),
        Index(
            "uq_active_camera_credential",
            "camera_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    credential_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_version: Mapped[int] = mapped_column(
        ForeignKey("credential_key_versions.version", ondelete="RESTRICT"), nullable=False
    )
    aad_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryRun(Base, IdMixin):
    __tablename__ = "discovery_runs"
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CameraObservation(Base, IdMixin):
    __tablename__ = "camera_observations"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), index=True
    )
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    result_code: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class CameraValidationAttempt(Base, IdMixin):
    __tablename__ = "camera_validation_attempts"
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("camera_stream_profiles.id", ondelete="RESTRICT")
    )
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("camera_credential_versions.id", ondelete="RESTRICT")
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    result_code: Mapped[str | None] = mapped_column(String(64))
    safe_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CameraStatus(Base):
    __tablename__ = "camera_status"
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), primary_key=True
    )
    validation_code: Mapped[str | None] = mapped_column(String(64))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_media_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SolutionDeployment(Base, IdMixin, TimeMixin):
    __tablename__ = "solution_deployments"
    __table_args__ = (UniqueConstraint("site_id", "deployment_key"),)
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    deployment_key: Mapped[str] = mapped_column(String(63), nullable=False)
    solution_id: Mapped[str] = mapped_column(String(63), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    registry: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle_intent: Mapped[str] = mapped_column(
        String(16), default="Running", nullable=False
    )
    next_desired_revision: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SolutionBundleRevision(Base, IdMixin):
    __tablename__ = "solution_bundle_revisions"
    __table_args__ = (UniqueConstraint("deployment_id", "bundle_sha256"),)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_deployments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DeploymentAssignmentSet(Base, IdMixin):
    __tablename__ = "deployment_assignment_sets"
    __table_args__ = (
        UniqueConstraint("deployment_id", "desired_revision"),
        UniqueConstraint("deployment_id", "idempotency_key"),
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_deployments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bundle_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_bundle_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    desired_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="committed", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CameraDeploymentAssignment(Base, IdMixin):
    __tablename__ = "camera_deployment_assignments"
    __table_args__ = (UniqueConstraint("assignment_set_id", "camera_id"),)
    assignment_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment_assignment_sets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False
    )
    stream_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera_stream_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("camera_credential_versions.id", ondelete="RESTRICT")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_fps: Mapped[int] = mapped_column(Integer, default=8, nullable=False)


class CameraApplicationAssignment(Base, IdMixin):
    __tablename__ = "camera_application_assignments"
    __table_args__ = (
        UniqueConstraint("camera_assignment_id", "bundle_application", "use_case_key"),
    )
    camera_assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera_deployment_assignments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bundle_application: Mapped[str] = mapped_column(String(63), nullable=False)
    use_case_key: Mapped[str] = mapped_column(String(63), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DeploymentSyncState(Base):
    __tablename__ = "deployment_sync_state"
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_deployments.id", ondelete="RESTRICT"), primary_key=True
    )
    desired_assignment_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment_assignment_sets.id", ondelete="RESTRICT"), nullable=False
    )
    applied_assignment_set_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployment_assignment_sets.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    claim_owner: Mapped[str | None] = mapped_column(String(200))
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ManagementOperation(Base, IdMixin):
    __tablename__ = "management_operations"
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DeploymentSyncAttempt(Base, IdMixin):
    __tablename__ = "deployment_sync_attempts"
    __table_args__ = (UniqueConstraint("deployment_id", "desired_revision", "attempt_number"),)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_deployments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assignment_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment_assignment_sets.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("management_operations.id", ondelete="RESTRICT"), nullable=False
    )
    desired_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_token: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    phase: Mapped[str] = mapped_column(String(32), default="claimed", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AlertInstance(Base, IdMixin):
    __tablename__ = "alert_instances"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical','warning','info')",
            name="alert_instance_severity",
        ),
        CheckConstraint(
            "state IN ('active','acknowledged','resolved')",
            name="alert_instance_state",
        ),
        Index("ix_alert_instances_state_last_seen", "state", "last_seen_at"),
    )
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    site_key: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_name: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    camera_key: Mapped[str | None] = mapped_column(String(63))
    use_case: Mapped[str | None] = mapped_column(String(63))
    state: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    occurrence_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_group_key: Mapped[str | None] = mapped_column(String(512))
    safe_labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    safe_annotations: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class AlertTransition(Base, IdMixin):
    __tablename__ = "alert_transitions"
    __table_args__ = (
        CheckConstraint(
            "transition_type IN ('firing','resolved')",
            name="alert_transition_type",
        ),
        Index("ix_alert_transitions_alert_received", "alert_id", "received_at"),
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_instances.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    transition_type: Mapped[str] = mapped_column(String(16), nullable=False)
    occurrence_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class NotificationPolicy(Base, IdMixin, TimeMixin):
    __tablename__ = "notification_policies"
    __table_args__ = (
        UniqueConstraint("site_key", "name"),
        CheckConstraint(
            "severity IS NULL OR severity IN ('critical','warning','info')",
            name="notification_policy_severity",
        ),
    )
    site_key: Mapped[str | None] = mapped_column(String(63), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16))
    alert_name: Mapped[str | None] = mapped_column(String(128))
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    repeat_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    send_resolved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class NotificationOutbox(Base, IdMixin):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "notification_type IN ('firing','reminder','resolved')",
            name="notification_outbox_type",
        ),
        CheckConstraint(
            "state IN ('pending','delivering','sent','failed','expired')",
            name="notification_outbox_state",
        ),
        Index("ix_notification_outbox_due", "state", "next_attempt_at"),
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_instances.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    transition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_transitions.id", ondelete="RESTRICT"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notification_policies.id", ondelete="RESTRICT"), nullable=False
    )
    notification_type: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationAttempt(Base, IdMixin):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        UniqueConstraint("outbox_id", "attempt_number"),
        CheckConstraint(
            "result IN ('sent','transient_failure','permanent_failure','expired')",
            name="notification_attempt_result",
        ),
    )
    outbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notification_outbox.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(64))


class AuditEvent(Base, IdMixin):
    __tablename__ = "audit_events"
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    request_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("management_operations.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class KubernetesResourceRef(Base, IdMixin):
    __tablename__ = "kubernetes_resource_refs"
    __table_args__ = (
        UniqueConstraint("sync_attempt_id", "api_version", "kind", "namespace", "name"),
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solution_deployments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sync_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment_sync_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    desired_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    api_version: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(253), nullable=False)
    uid: Mapped[str | None] = mapped_column(String(64))
    resource_version: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[int | None] = mapped_column(BigInteger)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class LegacyImport(Base, IdMixin):
    __tablename__ = "legacy_imports"
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
