"""Add the durable operational alert and notification outbox tables.

Revision ID: 0002_alerting_foundation
Revises: 0001_management_plane
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0002_alerting_foundation"
down_revision = "0001_management_plane"
branch_labels = None
depends_on = None


TABLES = (
    "alert_instances",
    "alert_transitions",
    "notification_policies",
    "notification_outbox",
    "notification_attempts",
)


def upgrade() -> None:
    # Revision 0001 uses metadata.create_all. checkfirst keeps a fresh install,
    # which sees current metadata in 0001, compatible with an upgrade from the
    # original 0001 schema.
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
