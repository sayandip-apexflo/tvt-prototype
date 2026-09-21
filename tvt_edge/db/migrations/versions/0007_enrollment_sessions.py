"""Add enrollment_camera_designations and enrollment_sessions tables.

Supersedes the enrollment_windows bookkeeping (0005) with a full durable
state machine (activating -> capturing -> restoring -> completed /
timed_out / cancelled / failed) plus an independent naming_status
(pending_name -> named) and a persisted designated enrollment camera per
deployment. enrollment_windows/EnrollmentWindow (0005) is left untouched and
still supported by ManagementService.start_enrollment/stop_enrollment for
backward compatibility; the operator UI now drives the richer session flow
in this migration instead. See docs/contracts/tvt-mills-v1/README.md and
tvt_edge/enrollment.py.

Revision ID: 0007_enrollment_sessions
Revises: 0006_camera_geometry_shapes
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0007_enrollment_sessions"
down_revision = "0006_camera_geometry_shapes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["enrollment_camera_designations"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["enrollment_sessions"].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["enrollment_sessions"].drop(bind=bind, checkfirst=True)
    Base.metadata.tables["enrollment_camera_designations"].drop(bind=bind, checkfirst=True)
