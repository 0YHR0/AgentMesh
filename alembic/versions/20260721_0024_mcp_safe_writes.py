"""Add governed MCP idempotent write authorizations.

Revision ID: 20260721_0024
Revises: 20260720_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260721_0024"
down_revision: str | None = "20260720_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_tool_invocations_status",
        "tool_invocations",
        "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
    )
    op.create_table(
        "tool_execution_authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("governed_action_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("server_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_digest", sa.String(length=80), nullable=False),
        sa.Column("tool_key", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("side_effect", sa.String(length=32), nullable=False),
        sa.Column("schema_digest", sa.String(length=80), nullable=False),
        sa.Column("arguments_digest", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "side_effect = 'IDEMPOTENT_WRITE'",
            name="ck_tool_execution_authorizations_side_effect",
        ),
        sa.CheckConstraint(
            "status IN ('AUTHORIZED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
            name="ck_tool_execution_authorizations_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["governed_action_id"], ["governed_actions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("governed_action_id"),
        sa.UniqueConstraint("invocation_id"),
    )
    op.create_index(
        "ix_tool_execution_authorizations_tenant_status",
        "tool_execution_authorizations",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    unknown_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM tool_invocations WHERE status = 'OUTCOME_UNKNOWN'")
    ).scalar_one()
    if unknown_count:
        raise RuntimeError(
            "Cannot downgrade while MCP Tool Invocations have OUTCOME_UNKNOWN status"
        )
    op.drop_index(
        "ix_tool_execution_authorizations_tenant_status",
        table_name="tool_execution_authorizations",
    )
    op.drop_table("tool_execution_authorizations")
    op.drop_constraint("ck_tool_invocations_status", "tool_invocations", type_="check")
