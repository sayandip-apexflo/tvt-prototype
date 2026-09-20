"""Add the enrollment_windows table for temporary face-enrollment cameras.

tvt-mills-pilot has no dedicated enrollment camera -- any face_recognition
camera can be temporarily switched to face_enrollment, then reverted. See
docs/contracts/tvt-mills-v1/README.md.

Revision ID: 0005_enrollment_windows
Revises: 0004_remove_camera_discovery
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0005_enrollment_windows"
down_revision = "0004_remove_camera_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["enrollment_windows"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["enrollment_windows"].drop(bind=op.get_bind(), checkfirst=True)
