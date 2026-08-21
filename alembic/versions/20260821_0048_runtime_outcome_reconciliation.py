"""Expand readers and storage for reconciled Runtime observation evidence."""

from alembic import op

revision = "20260821_0048"
down_revision = "20260820_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_runtime_observations_outcome", "runtime_observations", type_="check"
    )
    op.create_check_constraint(
        "ck_runtime_observations_outcome",
        "runtime_observations",
        "processing_outcome IN ('APPLIED', 'DUPLICATE', 'GAP', 'STALE_OWNER', "
        "'CONFLICT', 'RECONCILED')",
    )
    op.drop_constraint("ck_task_resolutions_action", "task_resolutions", type_="check")
    op.create_check_constraint(
        "ck_task_resolutions_action",
        "task_resolutions",
        "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME', "
        "'RECONCILE_MCP_SUCCEEDED', 'RECONCILE_MCP_FAILED', "
        "'BIND_A2A_REMOTE_TASK', 'RECONCILE_A2A_NOT_DELIVERED', "
        "'RECONCILE_RUNTIME_SUCCEEDED', 'RECONCILE_RUNTIME_FAILED', "
        "'RECONCILE_RUNTIME_CANCELED', 'RECONCILE_RUNTIME_TIMED_OUT')",
    )


def downgrade() -> None:
    # This cleanly reverses the compatibility release before any writer is
    # enabled. Once a writer has persisted RECONCILED, 0048 becomes the schema
    # floor and this constraint restoration intentionally fails closed.
    op.drop_constraint(
        "ck_runtime_observations_outcome", "runtime_observations", type_="check"
    )
    op.create_check_constraint(
        "ck_runtime_observations_outcome",
        "runtime_observations",
        "processing_outcome IN ('APPLIED', 'DUPLICATE', 'GAP', 'STALE_OWNER', "
        "'CONFLICT')",
    )
    op.drop_constraint("ck_task_resolutions_action", "task_resolutions", type_="check")
    op.create_check_constraint(
        "ck_task_resolutions_action",
        "task_resolutions",
        "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME', "
        "'RECONCILE_MCP_SUCCEEDED', 'RECONCILE_MCP_FAILED', "
        "'BIND_A2A_REMOTE_TASK', 'RECONCILE_A2A_NOT_DELIVERED')",
    )
