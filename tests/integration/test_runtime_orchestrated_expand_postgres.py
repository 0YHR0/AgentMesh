"""Real PostgreSQL checks for the A4.2a.0 expand-only schema floor."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentmesh.config import get_settings
from agentmesh.infrastructure.postgres.models import (
    PrincipalRecord,
    RuntimeExecutionRecord,
    RuntimeRegistrationRecord,
    RuntimeVersionRecord,
    TaskRecord,
    TaskRunRecord,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL migration tests",
    ),
]


def _config() -> Config:
    return Config("alembic.ini")


def _head_to_0048() -> None:
    command.upgrade(_config(), "head")
    command.downgrade(_config(), "20260821_0048")


def _create_runtime_execution(engine):
    from agentmesh.domain.runtime_execution import RuntimeExecution

    now = datetime.now(timezone.utc)
    tenant = f"runtime-expand-{uuid4().hex}"
    principal_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    registration_id, version_id = uuid4(), uuid4()
    execution_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            PrincipalRecord(
                id=principal_id,
                tenant_id=tenant,
                principal_type="SERVICE",
                status="ACTIVE",
                display_name="expand migration fixture",
                created_at=now,
                updated_at=now,
                revision=1,
            )
        )
        session.add(
            TaskRecord(
                id=task_id,
                tenant_id=tenant,
                project_id="migration",
                objective="migration fixture",
                input={},
                status="READY",
                current_run_id=None,
                output=None,
                error=None,
                execution_mode="DIRECT",
                acceptance_criteria=[],
                max_revisions=0,
                revision_count=0,
                review_deadline=None,
                candidate_output=None,
                latest_review=None,
                plan_version=None,
                plan_digest=None,
                max_concurrency=1,
                budget=None,
                settled_tokens=0,
                reserved_tokens=0,
                settled_cost_micros=0,
                reserved_cost_micros=0,
                budget_exhausted_reason=None,
                budget_revision=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                RuntimeRegistrationRecord(
                    id=registration_id,
                    tenant_id=None,
                    name=f"expand-{uuid4().hex}",
                    owner_principal_id=principal_id,
                    visibility="platform",
                    status="ACTIVE",
                    default_version_id=version_id,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
                RuntimeVersionRecord(
                    id=version_id,
                    runtime_id=registration_id,
                    api_version=1,
                    adapter_kind="python-in-process",
                    artifact_digest="a" * 64,
                    configuration_digest="b" * 64,
                    descriptor={"limits": {"max_assignment_bytes": 262144}},
                    trust_profile="built_in",
                    compatibility={},
                    status="PUBLISHED",
                    created_at=now,
                    published_at=now,
                    revoked_at=None,
                ),
                TaskRunRecord(
                    id=run_id,
                    task_id=task_id,
                    thread_id=str(run_id),
                    agent_id="migration-fixture",
                    agent_version_id=None,
                    agent_version_digest=None,
                    runtime_version_id=version_id,
                    runtime_execution_id=None,
                    runtime_execution_intent_id=None,
                    runtime_authority="legacy",
                    comparison_mode="off",
                    role="EXECUTOR",
                    revision_number=0,
                    subtask_id=None,
                    status="QUEUED",
                    output=None,
                    error=None,
                    queued_at=now,
                    started_at=None,
                    completed_at=None,
                    pause_requested_at=None,
                    paused_at=None,
                    resumed_at=None,
                    paused_from_status=None,
                ),
            ]
        )
        session.flush()
        execution = RuntimeExecution.prepare(
            execution_id=execution_id,
            tenant_id=tenant,
            run_id=run_id,
            runtime_version_id=version_id,
            assignment_id=uuid4(),
            assignment_digest="c" * 64,
            dispatch_key=f"migration:{uuid4()}",
            dispatch_digest="d" * 64,
            now=now,
        )
        session.add(
            RuntimeExecutionRecord(
                **{
                    "id": execution.id,
                    "tenant_id": execution.tenant_id,
                    "run_id": execution.run_id,
                    "runtime_version_id": execution.runtime_version_id,
                    "assignment_id": execution.assignment_id,
                    "assignment_digest": execution.assignment_digest,
                    "dispatch_key": execution.dispatch_key,
                    "dispatch_digest": execution.dispatch_digest,
                    "provider_execution_ref": None,
                    "provider_generation": None,
                    "phase": execution.phase.value,
                    "current_owner_attempt_id": None,
                    "current_fencing_token": None,
                    "provider_sequence": None,
                    "checkpoint_ref": None,
                    "workspace_ref": None,
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                    "terminal_at": None,
                }
            )
        )
    return execution


def _delete_runtime_execution(engine, execution) -> None:
    with engine.begin() as connection:
        task_id = connection.scalar(
            text("SELECT task_id FROM task_runs WHERE id = :id"), {"id": execution.run_id}
        )
        connection.execute(
            text("DELETE FROM runtime_lifecycle_operations WHERE runtime_execution_id = :id"),
            {"id": execution.id},
        )
        for table in (
            "runtime_assignment_snapshots",
            "runtime_handle_snapshots",
            "runtime_integrity_incidents",
        ):
            if inspect(connection).has_table(table):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE runtime_execution_id = :id"),
                    {"id": execution.id},
                )
        connection.execute(
            text("DELETE FROM runtime_executions WHERE id = :id"), {"id": execution.id}
        )
        if task_id is not None:
            connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
        connection.execute(
            text(
                "UPDATE runtime_registrations SET default_version_id = NULL "
                "WHERE name LIKE 'expand-%'"
            )
        )
        connection.execute(
            text(
                "DELETE FROM runtime_versions WHERE runtime_id IN "
                "(SELECT id FROM runtime_registrations WHERE name LIKE 'expand-%')"
            )
        )
        connection.execute(text("DELETE FROM runtime_registrations WHERE name LIKE 'expand-%'"))
        connection.execute(
            text(
                "DELETE FROM principals WHERE id NOT IN "
                "(SELECT owner_principal_id FROM runtime_registrations) "
                "AND tenant_id LIKE 'runtime-expand-%'"
            )
        )


def _insert_lifecycle(engine, execution, **extra) -> str:
    operation_id = f"migration-test:{uuid4()}"
    now = datetime.now(timezone.utc)
    values = {
        "id": uuid4(),
        "tenant_id": execution.tenant_id,
        "runtime_execution_id": execution.id,
        "operation_id": operation_id,
        "operation": "cancel",
        "intent_digest": "a" * 64,
        "status": "REQUESTED",
        "deadline": now + timedelta(hours=1),
        "receipt_summary": None,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        **extra,
    }
    columns = ", ".join(values)
    binds = ", ".join(f":{key}" for key in values)
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO runtime_lifecycle_operations ({columns}) VALUES ({binds})"),
            values,
        )
    return operation_id


def test_upgrade_backfills_zero_and_default_only_downgrade() -> None:
    engine = create_engine(get_settings().database_url)
    execution = None
    try:
        _head_to_0048()
        execution = _create_runtime_execution(engine)
        _insert_lifecycle(engine, execution)
        command.upgrade(_config(), "head")
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns WHERE "
                    "table_name='runtime_lifecycle_operations' AND column_name='attempt_count'"
                )
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM runtime_lifecycle_operations "
                        "WHERE attempt_count <> 0 OR next_attempt_at IS NOT NULL OR "
                        "claim_token IS NOT NULL OR claim_acquired_at IS NOT NULL OR "
                        "claim_expires_at IS NOT NULL OR last_error_code IS NOT NULL"
                    )
                )
                == 0
            )
        command.downgrade(_config(), "20260821_0048")
        with engine.connect() as connection:
            columns = {
                row.column_name
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='runtime_lifecycle_operations'"
                    )
                )
            }
            assert "attempt_count" not in columns
            assert not inspect(connection).has_table("runtime_assignment_snapshots")
            assert not inspect(connection).has_table("runtime_handle_snapshots")
            assert not inspect(connection).has_table("runtime_integrity_incidents")
    finally:
        if execution is not None:
            _delete_runtime_execution(engine, execution)
        command.upgrade(_config(), "head")
        engine.dispose()


@pytest.mark.parametrize(
    "marker,values",
    [
        ("attempt_count", {"attempt_count": 1}),
        ("next_attempt_at", {"next_attempt_at": datetime.now(timezone.utc)}),
        (
            "claim_token",
            {
                "claim_token": uuid4(),
                "claim_acquired_at": datetime.now(timezone.utc),
                "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
            },
        ),
        (
            "claim_acquired_at",
            {
                "claim_acquired_at": datetime.now(timezone.utc),
                "claim_token": uuid4(),
                "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
            },
        ),
        (
            "claim_expires_at",
            {
                "claim_expires_at": datetime.now(timezone.utc),
                "claim_token": uuid4(),
                "claim_acquired_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            },
        ),
        ("last_error_code", {"last_error_code": "runtime.transport"}),
    ],
)
def test_downgrade_refuses_each_lifecycle_writer_marker(
    marker: str, values: dict[str, object]
) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        _head_to_0048()
        command.upgrade(_config(), "head")
        execution = _create_runtime_execution(engine)
        _insert_lifecycle(engine, execution, **values)
        with pytest.raises(RuntimeError, match="writer markers"):
            command.downgrade(_config(), "20260821_0048")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM runtime_lifecycle_operations "
                    "WHERE operation_id LIKE 'migration-test:%'"
                )
            )
        _delete_runtime_execution(engine, execution)
        command.downgrade(_config(), "20260821_0048")
    finally:
        if "execution" in locals():
            _delete_runtime_execution(engine, execution)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_postgres_enforces_claim_triple_and_strict_expiry() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        _head_to_0048()
        command.upgrade(_config(), "head")
        execution = _create_runtime_execution(engine)
        now = datetime.now(timezone.utc)
        with pytest.raises(IntegrityError):
            _insert_lifecycle(engine, execution, claim_token=uuid4())
        with pytest.raises(IntegrityError):
            _insert_lifecycle(
                engine,
                execution,
                claim_token=uuid4(),
                claim_acquired_at=now,
                claim_expires_at=now,
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM runtime_lifecycle_operations "
                    "WHERE operation_id LIKE 'migration-test:%'"
                )
            )
        _delete_runtime_execution(engine, execution)
        command.upgrade(_config(), "head")
        engine.dispose()


@pytest.mark.parametrize(
    "table",
    [
        "runtime_assignment_snapshots",
        "runtime_handle_snapshots",
        "runtime_integrity_incidents",
    ],
)
def test_postgres_downgrade_refuses_each_new_table_row(table: str) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        _head_to_0048()
        command.upgrade(_config(), "head")
        execution = _create_runtime_execution(engine)
        now = datetime.now(timezone.utc)
        values: dict[str, object] = {
            "id": uuid4(),
            "tenant_id": execution.tenant_id,
            "runtime_execution_id": execution.id,
            "created_at": now,
        }
        if table == "runtime_assignment_snapshots":
            values.update(
                {
                    "contract_name": "agentmesh.runtime-assignment",
                    "contract_major": 1,
                    "assignment_id": uuid4(),
                    "assignment_digest": "a" * 64,
                    "canonical_payload": '{"bounded":true}',
                }
            )
        elif table == "runtime_handle_snapshots":
            values.update({"handle_digest": "b" * 64, "canonical_payload": '{"handle":true}'})
        else:
            values.update(
                {
                    "accepted_observation_id": "accepted",
                    "accepted_observation_digest": "c" * 64,
                    "accepted_phase": "SUCCEEDED",
                    "conflicting_observation_id": "conflict",
                    "conflicting_observation_digest": "d" * 64,
                    "conflicting_phase": "FAILED",
                    "status": "OPEN",
                    "reason": "migration test",
                    "updated_at": now,
                }
            )
        columns = ", ".join(values)
        binds = ", ".join(f":{key}" for key in values)
        with engine.begin() as connection:
            connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({binds})"), values)
        with pytest.raises(RuntimeError, match=f"{table} contains rows"):
            command.downgrade(_config(), "20260821_0048")
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": values["id"]})
        _delete_runtime_execution(engine, execution)
        command.downgrade(_config(), "20260821_0048")
    finally:
        if "execution" in locals():
            _delete_runtime_execution(engine, execution)
        command.upgrade(_config(), "head")
        engine.dispose()
