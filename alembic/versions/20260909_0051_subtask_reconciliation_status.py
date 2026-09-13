"""Allow readers to observe coordinated reconciliation-required Subtasks.

The status is intentionally reader-only in this revision.  No rows are
created or changed by the upgrade; the only schema change is replacement of
the Subtask status check constraint.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0051"
down_revision = "20260826_0050"
branch_labels = None
depends_on = None

_OLD_STATUS_CHECK = (
    "status IN ('BLOCKED', 'READY', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')"
)
_NEW_STATUS_CHECK = (
    "status IN ('BLOCKED', 'READY', 'RUNNING', 'RECONCILIATION_REQUIRED', "
    "'COMPLETED', 'FAILED', 'CANCELED')"
)


def upgrade() -> None:
    op.drop_constraint("ck_subtasks_status", "subtasks", type_="check")
    op.create_check_constraint("ck_subtasks_status", "subtasks", _NEW_STATUS_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM subtasks "
            "WHERE status = 'RECONCILIATION_REQUIRED' LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot downgrade 0051 to 0050: subtasks contain "
            "RECONCILIATION_REQUIRED rows; schema and data are unchanged"
        )
    op.drop_constraint("ck_subtasks_status", "subtasks", type_="check")
    op.create_check_constraint("ck_subtasks_status", "subtasks", _OLD_STATUS_CHECK)
