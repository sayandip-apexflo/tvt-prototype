"""Create the Slice 3 durable management schema.

Revision ID: 0001_management_plane
Revises: None
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0001_management_plane"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
