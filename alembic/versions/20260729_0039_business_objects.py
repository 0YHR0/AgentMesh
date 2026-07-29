"""Add typed Business Object registry and append-only revisions.

Revision ID: 20260729_0039
Revises: 20260729_0038
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0039"
down_revision = "20260729_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_object_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("json_schema", postgresql.JSONB(), nullable=False),
        sa.Column("lifecycle_definition", postgresql.JSONB(), nullable=False),
        sa.Column("sensitive_fields", postgresql.JSONB(), nullable=False),
        sa.Column("ownership_rules", postgresql.JSONB(), nullable=False),
        sa.Column("retention_policy", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "key",
            "schema_version",
            name="uq_business_object_types_company_key_version",
        ),
    )
    op.create_index(
        "uq_business_object_types_published",
        "business_object_types",
        ["company_id", "key"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )
    op.create_index(
        "ix_business_object_types_company_status",
        "business_object_types",
        ["company_id", "status"],
    )
    op.create_table(
        "business_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("type_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(255), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("lifecycle_state", sa.String(63), nullable=False),
        sa.Column("owner_position_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["type_id"], ["business_object_types.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_position_id"], ["company_positions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_business_objects_external_ref",
        "business_objects",
        ["type_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )
    op.create_index(
        "ix_business_objects_company_type_state",
        "business_objects",
        ["company_id", "type_id", "lifecycle_state"],
    )
    op.create_table(
        "business_object_revisions",
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(63), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("data_digest", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["object_id"], ["business_objects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "object_id", "revision", name="pk_business_object_revisions"
        ),
    )
    op.create_index(
        "ix_business_object_revisions_created",
        "business_object_revisions",
        ["object_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("business_object_revisions")
    op.drop_table("business_objects")
    op.drop_table("business_object_types")
