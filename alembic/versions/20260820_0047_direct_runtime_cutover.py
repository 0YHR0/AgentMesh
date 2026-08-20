"""Require immutable managed authority admission invariants."""

from alembic import op

revision = "20260820_0047"
down_revision = "20260817_0046"
branch_labels = None
depends_on = None


_LEGACY_COMPARISON_PIN = (
    "comparison_mode = 'off' OR (runtime_version_id IS NOT NULL AND "
    "(runtime_execution_id IS NOT NULL OR runtime_execution_intent_id IS NOT NULL))"
)
_MANAGED_AUTHORITY_PIN = (
    "runtime_authority = 'managed' AND comparison_mode = 'off' AND "
    "runtime_version_id IS NOT NULL AND (runtime_execution_id IS NOT NULL OR "
    "runtime_execution_intent_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint("ck_task_runs_comparison_pin", "task_runs", type_="check")
    op.create_check_constraint(
        "ck_task_runs_comparison_pin",
        "task_runs",
        f"({_MANAGED_AUTHORITY_PIN}) OR "
        f"(runtime_authority = 'legacy' AND ({_LEGACY_COMPARISON_PIN}))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_runs_comparison_pin", "task_runs", type_="check")
    op.create_check_constraint(
        "ck_task_runs_comparison_pin",
        "task_runs",
        "comparison_mode = 'off' OR (runtime_authority = 'legacy' AND "
        "runtime_version_id IS NOT NULL AND (runtime_execution_id IS NOT NULL OR "
        "runtime_execution_intent_id IS NOT NULL))",
    )
