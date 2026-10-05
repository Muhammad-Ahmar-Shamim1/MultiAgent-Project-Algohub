"""events: add status/requires_review (SRS 7.3), allow pool-level events

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("status", sa.String(length=32), nullable=True))
    op.add_column(
        "events",
        sa.Column("requires_review", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("events", "project_id", existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    op.alter_column("events", "project_id", existing_type=sa.String(length=32), nullable=False)
    op.drop_column("events", "requires_review")
    op.drop_column("events", "status")
