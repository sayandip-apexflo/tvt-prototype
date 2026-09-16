"""Remove the camera network-discovery and RTSP-validation subsystem.

Camera onboarding is now purely operator-driven (a manually entered RTSP
URL), matching the model used elsewhere in the ApexFabric fleet: the control
plane stores camera configuration only, and stream liveness is determined by
the CV pipeline pod that actually opens the stream at runtime.

Revision ID: 0004_remove_camera_discovery
Revises: 0003_solution_catalog
"""

import sqlalchemy as sa
from alembic import op


revision = "0004_remove_camera_discovery"
down_revision = "0003_solution_catalog"
branch_labels = None
depends_on = None


DROPPED_TABLES_IN_ORDER = (
    "camera_validation_attempts",
    "camera_observations",
    "discovery_runs",
    "discovery_scopes",
    "camera_status",
    "camera_onvif_config",
)


def upgrade() -> None:
    # A fresh install never creates these tables/column (0001 reflects current
    # models.py), so only an upgrade from a pre-0004 database has them.
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for name in DROPPED_TABLES_IN_ORDER:
        if name in existing_tables:
            op.drop_table(name)
    if "cameras" in existing_tables:
        camera_columns = {column["name"] for column in inspector.get_columns("cameras")}
        if "onboarding_state" in camera_columns:
            constraint_names = {
                constraint["name"]
                for constraint in inspector.get_check_constraints("cameras")
            }
            index_names = {index["name"] for index in inspector.get_indexes("cameras")}
            with op.batch_alter_table("cameras") as batch:
                if "camera_onboarding_state" in constraint_names:
                    batch.drop_constraint("camera_onboarding_state", type_="check")
                if "ix_cameras_onboarding_state" in index_names:
                    batch.drop_index("ix_cameras_onboarding_state")
                batch.drop_column("onboarding_state")


def downgrade() -> None:
    with op.batch_alter_table("cameras") as batch:
        batch.add_column(
            sa.Column(
                "onboarding_state",
                sa.String(32),
                nullable=False,
                server_default="discovered",
            )
        )
        batch.create_check_constraint(
            "camera_onboarding_state",
            "onboarding_state IN ('discovered','needs_credentials','validating',"
            "'online','offline','invalid','disabled','deleted')",
        )
        batch.create_index("ix_cameras_onboarding_state", ["onboarding_state"])
        batch.alter_column("onboarding_state", server_default=None)

    op.create_table(
        "camera_onvif_config",
        sa.Column("camera_id", sa.Uuid(), primary_key=True),
        sa.Column("device_endpoint_id", sa.Uuid(), nullable=True),
        sa.Column("media_service_path", sa.String(1024), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("last_queried_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["device_endpoint_id"], ["camera_endpoints.id"], ondelete="RESTRICT"
        ),
    )

    op.create_table(
        "camera_status",
        sa.Column("camera_id", sa.Uuid(), primary_key=True),
        sa.Column("validation_code", sa.String(64), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_media_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="RESTRICT"),
    )

    op.create_table(
        "discovery_scopes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("interface_name", sa.String(64), nullable=False),
        sa.Column("cidr", sa.String(64), nullable=False),
        sa.Column("rtsp_ports", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("site_id", "interface_name", "cidr"),
    )
    op.create_index(
        "ix_discovery_scopes_site_id", "discovery_scopes", ["site_id"]
    )

    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("counters", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_discovery_runs_site_id", "discovery_runs", ["site_id"])

    op.create_table(
        "camera_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("result_code", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["discovery_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_camera_observations_run_id", "camera_observations", ["run_id"])
    op.create_index("ix_camera_observations_camera_id", "camera_observations", ["camera_id"])
    op.create_index(
        "ix_camera_observations_observed_at", "camera_observations", ["observed_at"]
    )

    op.create_table(
        "camera_validation_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=True),
        sa.Column("credential_version_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("result_code", sa.String(64), nullable=True),
        sa.Column("safe_result", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["camera_stream_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credential_version_id"],
            ["camera_credential_versions.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_camera_validation_attempts_camera_id", "camera_validation_attempts", ["camera_id"]
    )
