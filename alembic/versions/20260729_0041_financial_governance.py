"""Add Company financial governance.

Revision ID: 20260729_0041
Revises: 20260729_0040
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0041"
down_revision = "20260729_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("parent_allocation_id", sa.Uuid(), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("approved_limit_micros", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "approved_limit_micros > 0", name="ck_budget_allocations_positive_limit"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_allocation_id"], ["budget_allocations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "scope_type", "scope_id", name="uq_budget_allocations_scope"
        ),
    )
    op.create_index(
        "ix_budget_allocations_company_status",
        "budget_allocations",
        ["company_id", "status"],
    )
    op.create_table(
        "budget_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_ref", sa.String(255), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_micros > 0", name="ck_budget_ledger_positive_amount"
        ),
        sa.ForeignKeyConstraint(
            ["allocation_id"], ["budget_allocations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "allocation_id",
            "operation_key",
            name="uq_budget_ledger_allocation_operation",
        ),
    )
    op.create_index(
        "ix_budget_ledger_allocation_created",
        "budget_ledger_entries",
        ["allocation_id", "created_at"],
    )
    op.create_table(
        "economic_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("verification", sa.String(16), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("external_ref", sa.String(255), nullable=True),
        sa.Column("source_snapshot_digest", sa.String(64), nullable=True),
        sa.Column("organization_unit_id", sa.Uuid(), nullable=True),
        sa.Column("initiative_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("attribution_method", sa.String(63), nullable=False),
        sa.Column("recorded_by", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_micros > 0", name="ck_economic_evidence_positive_amount"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"], ["organization_units.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["initiative_id"], ["company_initiatives.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["company_operations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "external_ref", name="uq_economic_evidence_external_ref"
        ),
    )
    op.create_index(
        "ix_economic_evidence_company_kind",
        "economic_evidence",
        ["company_id", "kind", "verification"],
    )
    op.create_table(
        "expense_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("vendor_ref", sa.String(255), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("risk_tier", sa.String(32), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "amount_micros > 0", name="ck_expense_requests_positive_amount"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["allocation_id"], ["budget_allocations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_expense_requests_company_status",
        "expense_requests",
        ["company_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("expense_requests")
    op.drop_table("economic_evidence")
    op.drop_table("budget_ledger_entries")
    op.drop_table("budget_allocations")
