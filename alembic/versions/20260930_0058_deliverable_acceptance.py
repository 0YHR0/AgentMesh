"""Add audited deliverable acceptance decisions without changing task execution status.

Revision ID: 20260930_0058
Revises: 20260930_0057
"""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0058"
down_revision = "20260930_0057"
branch_labels = None
depends_on = None

_OLD_ACTIONS = (
    "ACCEPT_CANDIDATE",
    "REJECT_TASK",
    "INCREASE_BUDGET_AND_RESUME",
    "RECONCILE_MCP_SUCCEEDED",
    "RECONCILE_MCP_FAILED",
    "BIND_A2A_REMOTE_TASK",
    "RECONCILE_A2A_NOT_DELIVERED",
    "RECONCILE_RUNTIME_SUCCEEDED",
    "RECONCILE_RUNTIME_FAILED",
    "RECONCILE_RUNTIME_CANCELED",
    "RECONCILE_RUNTIME_TIMED_OUT",
)
_NEW_ACTIONS = ("ACCEPT_DELIVERABLE", "REJECT_DELIVERABLE")


def _replace_constraint(actions: tuple[str, ...]) -> None:
    op.drop_constraint("ck_task_resolutions_action", "task_resolutions", type_="check")
    op.create_check_constraint(
        "ck_task_resolutions_action",
        "task_resolutions",
        "action IN (" + ", ".join(repr(action) for action in actions) + ")",
    )


def upgrade() -> None:
    _replace_constraint(_OLD_ACTIONS + _NEW_ACTIONS)


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM task_resolutions "
                "WHERE action IN ('ACCEPT_DELIVERABLE', 'REJECT_DELIVERABLE')"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError("Cannot downgrade while deliverable acceptance audit records exist")
    _replace_constraint(_OLD_ACTIONS)
