"""Add the camera_geometry_shapes table for ANPR zones and entry/exit lines.

Operator-drawn geometry (normalized 0-1 image coordinates) that
tvt_edge/geometry.py compiles into the vendor pack's
config.zones.anpr[]/config.lines[] shape. See
docs/contracts/tvt-mills-v1/README.md for the two-lines-per-gate direction
convention this table follows.

Revision ID: 0006_camera_geometry_shapes
Revises: 0005_enrollment_windows
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0006_camera_geometry_shapes"
down_revision = "0005_enrollment_windows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["camera_geometry_shapes"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["camera_geometry_shapes"].drop(bind=op.get_bind(), checkfirst=True)
