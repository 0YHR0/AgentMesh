"""Allow audited MCP and A2A outcome reconciliation actions.

Revision ID: 20260721_0028
Revises: 20260721_0027
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260721_0028"
down_revision: str | None = "20260721_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_task_resolutions_action", "task_resolutions", type_="check")
    op.create_check_constraint(
        "ck_task_resolutions_action",
        "task_resolutions",
        "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME', "
        "'RECONCILE_MCP_SUCCEEDED', 'RECONCILE_MCP_FAILED', "
        "'BIND_A2A_REMOTE_TASK', 'RECONCILE_A2A_NOT_DELIVERED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_resolutions_action", "task_resolutions", type_="check")
    op.execute(
        "DELETE FROM task_resolutions WHERE action IN "
        "('RECONCILE_MCP_SUCCEEDED', 'RECONCILE_MCP_FAILED', "
        "'BIND_A2A_REMOTE_TASK', 'RECONCILE_A2A_NOT_DELIVERED')"
    )
    op.create_check_constraint(
        "ck_task_resolutions_action",
        "task_resolutions",
        "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME')",
    )
