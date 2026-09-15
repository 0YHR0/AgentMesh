"""Add the append-only Runtime integrity incident action ledger.

This is an expand-compatible follow-up to 0049.  It tightens the accepted
terminal phase constraint to match the domain's business-accepted phases.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260826_0050"
down_revision = "20260825_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_runtime_integrity_incident_terminal_phases",
        "runtime_integrity_incidents",
        type_="check",
    )
    op.drop_constraint(
        "ck_runtime_integrity_incident_digests",
        "runtime_integrity_incidents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_runtime_integrity_incident_digests",
        "runtime_integrity_incidents",
        "accepted_observation_digest ~ '^[0-9a-f]{64}$' AND "
        "conflicting_observation_digest ~ '^[0-9a-f]{64}$' AND "
        "accepted_observation_digest <> conflicting_observation_digest",
    )
    op.create_check_constraint(
        "ck_runtime_integrity_incident_terminal_phases",
        "runtime_integrity_incidents",
        "accepted_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT') AND "
        "conflicting_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST')",
    )
    op.create_check_constraint(
        "ck_runtime_integrity_incident_timestamps",
        "runtime_integrity_incidents",
        "updated_at >= created_at",
    )
    op.create_table(
        "runtime_integrity_incident_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_integrity_incidents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=False),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=4096), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('ACKNOWLEDGE', 'ESCALATE')", name="ck_runtime_incident_action"
        ),
        sa.CheckConstraint(
            "(action = 'ACKNOWLEDGE' AND from_status = 'OPEN' "
            "AND to_status = 'ACKNOWLEDGED') OR "
            "(action = 'ESCALATE' AND from_status IN ('OPEN', 'ACKNOWLEDGED') "
            "AND to_status = 'ESCALATED')",
            name="ck_runtime_incident_action_status",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name="ck_runtime_incident_action_digest"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "incident_id",
            "request_digest",
            name="uq_runtime_incident_action_request",
        ),
    )
    op.create_index(
        "ix_runtime_incident_actions_tenant_created",
        "runtime_integrity_incident_actions",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_runtime_incident_actions_incident_created",
        "runtime_integrity_incident_actions",
        ["incident_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT 1 FROM runtime_integrity_incident_actions LIMIT 1")
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot downgrade 0050: runtime integrity incident actions contain rows"
        )
    op.drop_index(
        "ix_runtime_incident_actions_incident_created",
        table_name="runtime_integrity_incident_actions",
    )
    op.drop_index(
        "ix_runtime_incident_actions_tenant_created",
        table_name="runtime_integrity_incident_actions",
    )
    op.drop_table("runtime_integrity_incident_actions")
    op.drop_constraint(
        "ck_runtime_integrity_incident_timestamps",
        "runtime_integrity_incidents",
        type_="check",
    )
    op.drop_constraint(
        "ck_runtime_integrity_incident_terminal_phases",
        "runtime_integrity_incidents",
        type_="check",
    )
    op.drop_constraint(
        "ck_runtime_integrity_incident_digests",
        "runtime_integrity_incidents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_runtime_integrity_incident_digests",
        "runtime_integrity_incidents",
        "accepted_observation_digest ~ '^[0-9a-f]{64}$' AND "
        "conflicting_observation_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_runtime_integrity_incident_terminal_phases",
        "runtime_integrity_incidents",
        "accepted_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST') AND "
        "conflicting_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST')",
    )
