"""approvals: request/decision linkage, idempotency key, evidence, created_at (FR-130..FR-132)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("decides_id", sa.String(length=32), nullable=True))
    op.add_column("approvals", sa.Column("request_key", sa.String(length=128), nullable=True))
    op.add_column(
        "approvals",
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "approvals",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_foreign_key(
        op.f("fk_approvals_decides_id_approvals"), "approvals", "approvals", ["decides_id"], ["id"]
    )
    op.create_index(
        "uq_approvals_one_decision_per_request",
        "approvals",
        ["decides_id"],
        unique=True,
        postgresql_where=sa.text("decides_id IS NOT NULL"),
    )
    op.create_index(
        "uq_approvals_request_key_per_project",
        "approvals",
        ["project_id", "request_key"],
        unique=True,
        postgresql_where=sa.text("request_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_request_key_per_project", table_name="approvals")
    op.drop_index("uq_approvals_one_decision_per_request", table_name="approvals")
    op.drop_constraint(op.f("fk_approvals_decides_id_approvals"), "approvals", type_="foreignkey")
    op.drop_column("approvals", "created_at")
    op.drop_column("approvals", "evidence")
    op.drop_column("approvals", "request_key")
    op.drop_column("approvals", "decides_id")
