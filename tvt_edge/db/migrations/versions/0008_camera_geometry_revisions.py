"""Track authoritative camera geometry revisions in assignment snapshots.

Revision ID: 0008_camera_geometry_revisions
Revises: 0007_enrollment_sessions
"""

import sqlalchemy as sa
from alembic import op


revision = "0008_camera_geometry_revisions"
down_revision = "0007_enrollment_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("geometry_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "camera_deployment_assignments",
        sa.Column("geometry_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.alter_column("cameras", "geometry_revision", server_default=None)
    op.alter_column(
        "camera_deployment_assignments", "geometry_revision", server_default=None
    )


def downgrade() -> None:
    op.drop_column("camera_deployment_assignments", "geometry_revision")
    op.drop_column("cameras", "geometry_revision")
