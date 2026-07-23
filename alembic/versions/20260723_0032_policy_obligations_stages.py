"""policy obligations and staged approvals

Revision ID: 20260723_0032
Revises: 20260723_0031
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260723_0032"
down_revision = "20260723_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "governed_actions",
        sa.Column(
            "obligations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "governed_actions",
        sa.Column(
            "approval_stages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(
                r"""'[{"name":"approval","quorum"\:1,"eligible_roles":[]}]'::jsonb"""
            ),
        ),
    )
    op.add_column(
        "governed_actions",
        sa.Column("current_stage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "approval_decisions",
        sa.Column("stage", sa.String(length=64), nullable=False, server_default="approval"),
    )
    op.drop_constraint(
        "uq_approval_decisions_approval", "approval_decisions", type_="unique"
    )
    op.create_unique_constraint(
        "uq_approval_decisions_stage_approver",
        "approval_decisions",
        ["approval_id", "stage", "approver_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_approval_decisions_stage_approver",
        "approval_decisions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_approval_decisions_approval", "approval_decisions", ["approval_id"]
    )
    op.drop_column("approval_decisions", "stage")
    op.drop_column("governed_actions", "current_stage")
    op.drop_column("governed_actions", "approval_stages")
    op.drop_column("governed_actions", "obligations")
