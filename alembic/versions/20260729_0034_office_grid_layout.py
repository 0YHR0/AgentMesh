"""Add durable grid-based Office employee placements.

Revision ID: 20260729_0034
Revises: 20260723_0033
"""

import sqlalchemy as sa

from alembic import op

revision = "20260729_0034"
down_revision = "20260723_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "office_employee_placements",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("grid_x", sa.Integer(), nullable=False),
        sa.Column("grid_z", sa.Integer(), nullable=False),
        sa.Column("department", sa.String(length=63), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("grid_x >= 0 AND grid_x < 35", name="ck_office_grid_x"),
        sa.CheckConstraint("grid_z >= 0 AND grid_z < 12", name="ck_office_grid_z"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "agent_id", name="pk_office_employee_placements"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "grid_x",
            "grid_z",
            name="uq_office_employee_placements_cell",
        ),
    )
    op.create_index(
        "ix_office_employee_placements_department",
        "office_employee_placements",
        ["tenant_id", "department"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_office_employee_placements_department",
        table_name="office_employee_placements",
    )
    op.drop_table("office_employee_placements")
