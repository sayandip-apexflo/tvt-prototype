"""Add the PostgreSQL-backed immutable solution catalog.

Revision ID: 0003_solution_catalog
Revises: 0002_alerting_foundation
"""

from alembic import op

from tvt_edge.db.models import Base


revision = "0003_solution_catalog"
down_revision = "0002_alerting_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 creates current metadata on a fresh install. checkfirst also
    # supports upgrading databases created from the original 0001/0002 schema.
    Base.metadata.tables["solution_catalog_entries"].create(
        bind=op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    Base.metadata.tables["solution_catalog_entries"].drop(
        bind=op.get_bind(), checkfirst=True
    )
