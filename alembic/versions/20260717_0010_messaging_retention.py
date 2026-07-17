"""Add messaging retention invariants and indexes.

Revision ID: 20260717_0010
Revises: 20260717_0009
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260717_0010"
down_revision: str | None = "20260717_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_outbox_published_timestamp",
        "outbox_events",
        "(status = 'PUBLISHED' AND published_at IS NOT NULL) "
        "OR (status <> 'PUBLISHED' AND published_at IS NULL)",
    )
    op.create_index(
        "ix_outbox_published_retention",
        "outbox_events",
        ["published_at", "id"],
        postgresql_where=sa.text("status = 'PUBLISHED' AND published_at IS NOT NULL"),
    )
    op.create_index(
        "ix_inbox_retention",
        "inbox_messages",
        ["processed_at", "tenant_id", "consumer_name", "message_id"],
    )
    op.drop_index("ix_inbox_processed_at", table_name="inbox_messages")
    op.drop_constraint("inbox_messages_pkey", "inbox_messages", type_="primary")
    op.create_primary_key(
        "inbox_messages_pkey",
        "inbox_messages",
        ["tenant_id", "consumer_name", "message_id"],
    )


def downgrade() -> None:
    conflicting_key = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT consumer_name, message_id "
                "FROM inbox_messages "
                "GROUP BY consumer_name, message_id "
                "HAVING COUNT(*) > 1 "
                "LIMIT 1"
            )
        )
        .first()
    )
    if conflicting_key is not None:
        raise RuntimeError(
            "Cannot downgrade 20260717_0010 while multiple tenants share an "
            "Inbox (consumer_name, message_id); resolve those records before "
            "restoring the legacy two-column primary key."
        )
    op.drop_constraint("inbox_messages_pkey", "inbox_messages", type_="primary")
    op.create_primary_key(
        "inbox_messages_pkey",
        "inbox_messages",
        ["consumer_name", "message_id"],
    )
    op.create_index(
        "ix_inbox_processed_at",
        "inbox_messages",
        ["processed_at"],
    )
    op.drop_index("ix_inbox_retention", table_name="inbox_messages")
    op.drop_index("ix_outbox_published_retention", table_name="outbox_events")
    op.drop_constraint(
        "ck_outbox_published_timestamp",
        "outbox_events",
        type_="check",
    )
