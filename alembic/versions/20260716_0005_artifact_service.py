"""Add inline-small Artifact metadata and immutable Versions.

Revision ID: 20260716_0005
Revises: 20260716_0004
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260716_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("version_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_artifacts_revision"),
        sa.CheckConstraint("version_count >= 0", name="ck_artifacts_version_count"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_tenant_created", "artifacts", ["tenant_id", "created_at"])
    op.create_index("ix_artifacts_tenant_kind", "artifacts", ["tenant_id", "kind"])

    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_class", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scan_status", sa.String(length=32), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(content) = size_bytes",
            name="ck_artifact_versions_content_size",
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_artifact_versions_number"),
        sa.CheckConstraint("size_bytes >= 1", name="ck_artifact_versions_size"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["producer_run_id"], ["task_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "version_number",
            name="uq_artifact_version_number",
        ),
    )
    op.create_index(
        "ix_artifact_versions_artifact_created",
        "artifact_versions",
        ["artifact_id", "created_at"],
    )
    op.create_index("ix_artifact_versions_sha256", "artifact_versions", ["sha256"])
    op.create_index(
        "ix_artifact_versions_producer_run",
        "artifact_versions",
        ["producer_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_versions_producer_run", table_name="artifact_versions")
    op.drop_index("ix_artifact_versions_sha256", table_name="artifact_versions")
    op.drop_index("ix_artifact_versions_artifact_created", table_name="artifact_versions")
    op.drop_table("artifact_versions")
    op.drop_index("ix_artifacts_tenant_kind", table_name="artifacts")
    op.drop_index("ix_artifacts_tenant_created", table_name="artifacts")
    op.drop_table("artifacts")
