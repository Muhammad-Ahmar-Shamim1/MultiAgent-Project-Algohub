"""war_rooms + war_room_members (FR-080)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "war_rooms",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("required_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('open', 'archived')", name=op.f("ck_war_rooms_status")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_war_rooms_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_war_rooms")),
    )
    op.create_index(op.f("ix_war_rooms_project_id"), "war_rooms", ["project_id"], unique=False)
    op.create_index(
        "uq_war_rooms_one_open_per_project",
        "war_rooms",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "war_room_members",
        sa.Column("war_room_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_war_room_members_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_war_room_members_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["war_room_id"],
            ["war_rooms.id"],
            name=op.f("fk_war_room_members_war_room_id_war_rooms"),
        ),
        sa.PrimaryKeyConstraint("war_room_id", "agent_id", name=op.f("pk_war_room_members")),
    )
    op.create_index(
        op.f("ix_war_room_members_project_id"), "war_room_members", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_war_room_members_project_id"), table_name="war_room_members")
    op.drop_table("war_room_members")
    op.drop_index("uq_war_rooms_one_open_per_project", table_name="war_rooms")
    op.drop_index(op.f("ix_war_rooms_project_id"), table_name="war_rooms")
    op.drop_table("war_rooms")
