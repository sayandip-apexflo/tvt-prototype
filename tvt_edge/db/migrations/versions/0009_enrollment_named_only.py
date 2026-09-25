"""Enrollment creates a person only when the operator names it.

Adds enrollment_sessions.capture_event_ids (staged apexfabric-control
capture IDs) and the 'discarded' naming_status. Sessions still pending a
name from the old auto-enrollment flow are marked discarded: their unnamed
person records are removed by `tvt-edge-operations.sh purge-unnamed-persons`.

Revision ID: 0009_enrollment_named_only
Revises: 0008_camera_geometry_revisions
"""

import sqlalchemy as sa
from alembic import op


revision = "0009_enrollment_named_only"
down_revision = "0008_camera_geometry_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enrollment_sessions", sa.Column("capture_event_ids", sa.JSON(), nullable=True))
    op.drop_constraint("enrollment_session_naming_status", "enrollment_sessions", type_="check")
    op.create_check_constraint(
        "enrollment_session_naming_status",
        "enrollment_sessions",
        "naming_status IN ('not_applicable','pending_name','named','discarded')",
    )
    op.execute(
        "UPDATE enrollment_sessions SET naming_status = 'discarded' WHERE naming_status = 'pending_name'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE enrollment_sessions SET naming_status = 'not_applicable' WHERE naming_status = 'discarded'"
    )
    op.drop_constraint("enrollment_session_naming_status", "enrollment_sessions", type_="check")
    op.create_check_constraint(
        "enrollment_session_naming_status",
        "enrollment_sessions",
        "naming_status IN ('not_applicable','pending_name','named')",
    )
    op.drop_column("enrollment_sessions", "capture_event_ids")
