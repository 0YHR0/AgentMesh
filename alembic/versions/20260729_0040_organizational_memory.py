"""Add governed Organizational Memory.

Revision ID: 20260729_0040
Revises: 20260729_0039
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0040"
down_revision = "20260729_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("readable_namespace_patterns", postgresql.JSONB(), nullable=False),
        sa.Column("writable_namespace_patterns", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_memory_types", postgresql.JSONB(), nullable=False),
        sa.Column("auto_accept_memory_types", postgresql.JSONB(), nullable=False),
        sa.Column("forbidden_sensitivity_levels", postgresql.JSONB(), nullable=False),
        sa.Column("maximum_retrieval_count", sa.Integer(), nullable=False),
        sa.Column("maximum_context_tokens", sa.Integer(), nullable=False),
        sa.Column("default_ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("review_role", sa.String(128), nullable=False),
        sa.Column("extraction_enabled", sa.Boolean(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "key",
            "version",
            name="uq_memory_policies_company_key_version",
        ),
    )
    op.create_index(
        "uq_memory_policies_active",
        "memory_policies",
        ["company_id", "key"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.create_table(
        "memory_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("namespace_type", sa.String(32), nullable=False),
        sa.Column("namespace_id", sa.String(255), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("provenance_type", sa.String(32), nullable=False),
        sa.Column("provenance_id", sa.String(255), nullable=False),
        sa.Column("proposed_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("confidence_basis_points", sa.Integer(), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "confidence_basis_points >= 0 AND confidence_basis_points <= 10000",
            name="ck_memory_records_confidence",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposed_by_run_id"], ["task_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["memory_records.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_records_search",
        "memory_records",
        ["company_id", "namespace_type", "namespace_id", "memory_type", "status"],
    )
    op.create_index(
        "ix_memory_records_expiry", "memory_records", ["status", "expires_at"]
    )
    op.create_table(
        "memory_evidence",
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(63), nullable=False),
        sa.Column("evidence_id", sa.String(255), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memory_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "memory_id",
            "evidence_type",
            "evidence_id",
            name="pk_memory_evidence",
        ),
    )
    op.create_table(
        "memory_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memory_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_reviews_memory_created",
        "memory_reviews",
        ["memory_id", "created_at"],
    )
    op.create_table(
        "memory_retrievals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("query_digest", sa.String(64), nullable=False),
        sa.Column("namespace_keys", postgresql.JSONB(), nullable=False),
        sa.Column("memory_types", postgresql.JSONB(), nullable=False),
        sa.Column("result_memory_ids", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["memory_policies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_retrievals_task_created",
        "memory_retrievals",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_memory_retrievals_run_created",
        "memory_retrievals",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_memory_retrievals_company_created",
        "memory_retrievals",
        ["company_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("memory_retrievals")
    op.drop_table("memory_reviews")
    op.drop_table("memory_evidence")
    op.drop_table("memory_records")
    op.drop_table("memory_policies")
