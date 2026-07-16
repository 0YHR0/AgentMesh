"""Normalize legacy JSON columns to JSONB.

Revision ID: 20260716_0003
Revises: 20260716_0002
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260716_0003"
down_revision: str | None = "20260716_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _database_type(table_name: str, column_name: str) -> str | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table_name AND column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        .scalar_one_or_none()
    )


def upgrade() -> None:
    for table_name, column_name, nullable in (
        ("tasks", "input", False),
        ("tasks", "output", True),
        ("task_runs", "output", True),
    ):
        if _database_type(table_name, column_name) == "json":
            op.alter_column(
                table_name,
                column_name,
                existing_type=postgresql.JSON(),
                type_=postgresql.JSONB(),
                existing_nullable=nullable,
                postgresql_using=f"{column_name}::jsonb",
            )


def downgrade() -> None:
    # JSONB is backwards-compatible with the application contract; avoid a lossy table rewrite.
    pass
