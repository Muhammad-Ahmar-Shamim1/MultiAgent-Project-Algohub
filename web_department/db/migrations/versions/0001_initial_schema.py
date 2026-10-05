"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-21 21:39:01.115027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "blueprints",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", "version", name=op.f("pk_blueprints")),
    )
    op.create_table(
        "projects",
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("requester_id", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "RECEIVED",
                "ANALYZING",
                "PLANNING",
                "ARCHITECTING",
                "DEVELOPING",
                "INTEGRATING",
                "TESTING",
                "SECURITY_REVIEW",
                "AWAITING_APPROVAL",
                "DEPLOYING",
                "MONITORING",
                "COMPLETED",
                "FAILED",
                "BLOCKED",
                name="projectstate",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("wrs_version", sa.Integer(), nullable=True),
        sa.Column("stack", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "approvals",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approver_id", sa.String(length=128), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_approvals_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(op.f("ix_approvals_project_id"), "approvals", ["project_id"], unique=False)
    op.create_table(
        "deployments",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("health", sa.String(length=32), nullable=True),
        sa.Column("approver_id", sa.String(length=128), nullable=True),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_deployments_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.create_index(op.f("ix_deployments_project_id"), "deployments", ["project_id"], unique=False)
    op.create_table(
        "knowledge_entries",
        sa.Column("layer", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("source", sa.String(length=500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_knowledge_entries_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_entries")),
    )
    op.create_index(
        op.f("ix_knowledge_entries_project_id"), "knowledge_entries", ["project_id"], unique=False
    )
    op.create_table(
        "requirement_specs",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_requirement_specs_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requirement_specs")),
        sa.UniqueConstraint("project_id", "version", name=op.f("uq_requirement_specs_project_id")),
    )
    op.create_index(
        op.f("ix_requirement_specs_project_id"), "requirement_specs", ["project_id"], unique=False
    )
    op.create_table(
        "workstations",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("repo_path", sa.String(length=500), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_workstations_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workstations")),
    )
    op.create_index(
        op.f("ix_workstations_project_id"), "workstations", ["project_id"], unique=False
    )
    op.create_table(
        "agents",
        sa.Column("display_id", sa.String(length=32), nullable=False),
        sa.Column("blueprint_id", sa.String(length=100), nullable=False),
        sa.Column("blueprint_version", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "CREATED",
                "PROVISIONED",
                "AVAILABLE",
                "ASSIGNED",
                "WORKING",
                "REVIEW",
                "BLOCKED",
                "RETIRED",
                name="agentstate",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("workstation_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_id", "blueprint_version"],
            ["blueprints.id", "blueprints.version"],
            name=op.f("fk_agents_blueprint_id_blueprints"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_agents_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["workstation_id"],
            ["workstations.id"],
            name=op.f("fk_agents_workstation_id_workstations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("display_id", name=op.f("uq_agents_display_id")),
    )
    op.create_index(op.f("ix_agents_project_id"), "agents", ["project_id"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("display_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "CREATED",
                "DEPENDENCY_WAITING",
                "READY",
                "ASSIGNED",
                "IN_PROGRESS",
                "REVIEW",
                "FIX_REQUIRED",
                "COMPLETED",
                "BLOCKED",
                "CANCELLED",
                name="taskstate",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("acceptance_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("assigned_agent_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"], ["agents.id"], name=op.f("fk_tasks_assigned_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_tasks_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("project_id", "display_id", name=op.f("uq_tasks_project_id")),
    )
    op.create_index(
        op.f("ix_tasks_assigned_agent_id"), "tasks", ["assigned_agent_id"], unique=False
    )
    op.create_index(op.f("ix_tasks_project_id"), "tasks", ["project_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_events_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_events_project_id_projects")
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_events_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index(op.f("ix_events_project_id"), "events", ["project_id"], unique=False)
    op.create_index(op.f("ix_events_type"), "events", ["type"], unique=False)
    op.create_table(
        "llm_calls",
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_llm_calls_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_llm_calls_project_id_projects")
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_llm_calls_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(op.f("ix_llm_calls_project_id"), "llm_calls", ["project_id"], unique=False)
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("depends_on_id", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "task_id <> depends_on_id", name=op.f("ck_task_dependencies_no_self_dependency")
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_id"], ["tasks.id"], name=op.f("fk_task_dependencies_depends_on_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_dependencies_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("task_id", "depends_on_id", name=op.f("pk_task_dependencies")),
    )
    op.create_index(
        op.f("ix_task_dependencies_depends_on_id"),
        "task_dependencies",
        ["depends_on_id"],
        unique=False,
    )

    # DR-003: events and approvals are append-only. The ORM also guards this (db/models.py).
    op.execute(
        """
        CREATE FUNCTION reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% on % is not allowed: table is append-only', TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("events", "approvals"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )


def downgrade() -> None:
    for table in ("events", "approvals"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_mutation()")
    op.drop_index(op.f("ix_task_dependencies_depends_on_id"), table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_index(op.f("ix_llm_calls_project_id"), table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(op.f("ix_events_type"), table_name="events")
    op.drop_index(op.f("ix_events_project_id"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_tasks_project_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_assigned_agent_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_agents_project_id"), table_name="agents")
    op.drop_table("agents")
    op.drop_index(op.f("ix_workstations_project_id"), table_name="workstations")
    op.drop_table("workstations")
    op.drop_index(op.f("ix_requirement_specs_project_id"), table_name="requirement_specs")
    op.drop_table("requirement_specs")
    op.drop_index(op.f("ix_knowledge_entries_project_id"), table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
    op.drop_index(op.f("ix_deployments_project_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(op.f("ix_approvals_project_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("projects")
    op.drop_table("blueprints")
    op.execute("DROP EXTENSION IF EXISTS vector")
