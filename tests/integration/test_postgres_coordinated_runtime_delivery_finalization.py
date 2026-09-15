"""Real PostgreSQL qualification for c2f5e delivery finalization."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
    CoordinatedUnknownOutcomeKind,
)
from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import (
    InboxMessageRecord,
    OutboxEventRecord,
    RuntimeExecutionRecord,
    RuntimeObservationRecord,
)
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyInboxRepository
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWork
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _cleanup as _convergence_cleanup,
)
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _real_scheduler,
    _running_fixture,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run c2f5e PostgreSQL tests",
    ),
]

CONSUMER = "c2f5e-delivery-consumer"


def _engine():
    settings = get_settings()
    seed_builtin_registry(settings)
    return create_engine(os.environ.get("AGENTMESH_DATABASE_URL", settings.database_url))


def _terminal_service(fixture, engine):
    return CoordinatedRuntimeConvergenceService(
        uow_factory=fixture.factory,
        coordinated_scheduler=_real_scheduler(engine, fixture),
        cancel_deadline_window=timedelta(minutes=5),
    )


def _unknown_service(fixture):
    return CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=fixture.factory,
        cancel_deadline_window=timedelta(minutes=5),
    )


def _envelope(fixture) -> MessageEnvelope:
    return MessageEnvelope.run_requested(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        at=fixture.now,
    )


def _known_observation(execution, observed_at):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=observed_at,
        provider_event_id=f"c2f5e-known-{uuid4().hex}",
        provider_sequence=2,
        output={"result": "ok"},
    )


def _unknown_observation(execution, observed_at):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=observed_at,
        provider_event_id=f"c2f5e-unknown-{uuid4().hex}",
        provider_sequence=2,
    )


def _counts(engine, fixture, execution_id):
    with Session(engine) as session:
        return (
            len(
                session.scalars(
                    select(RuntimeObservationRecord).where(
                        RuntimeObservationRecord.runtime_execution_id == execution_id
                    )
                ).all()
            ),
            len(
                session.scalars(
                    select(InboxMessageRecord).where(
                        InboxMessageRecord.tenant_id == fixture.tenant_id,
                        InboxMessageRecord.consumer_name == CONSUMER,
                    )
                ).all()
            ),
            len(
                session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.tenant_id == fixture.tenant_id
                    )
                ).all()
            ),
        )


def _business_projection(engine, fixture, execution_id):
    """Read the complete mutable chain from a fresh connection for rollback checks."""
    with engine.connect() as connection:
        parameters = {
            "task_id": fixture.task.id,
            "run_id": fixture.run.id,
            "attempt_id": fixture.attempt.id,
            "execution_id": execution_id,
        }
        return {
            "task": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, output, candidate_output, "
                        "budget_exhausted_reason, reserved_tokens, settled_tokens, "
                        "reserved_cost_micros, settled_cost_micros, version "
                        "FROM tasks WHERE id = :task_id"
                    ),
                    parameters,
                ).one()
            ),
            "run": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, output, completed_at, "
                        "pause_requested_at, paused_at, resumed_at "
                        "FROM task_runs WHERE id = :run_id"
                    ),
                    parameters,
                ).one()
            ),
            "attempt": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, completed_at, settled_tokens, "
                        "settled_cost_micros, budget_settlement_source, "
                        "lease_expires_at, heartbeat_at "
                        "FROM task_attempts WHERE id = :attempt_id"
                    ),
                    parameters,
                ).one()
            ),
            "execution": tuple(
                connection.execute(
                    text(
                        "SELECT phase, provider_sequence, checkpoint_ref, workspace_ref, version "
                        "FROM runtime_executions WHERE id = :execution_id"
                    ),
                    parameters,
                ).one()
            ),
            "subtask": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, output, current_run_id, version "
                        "FROM subtasks WHERE id = ("
                        "SELECT subtask_id FROM task_runs WHERE id = :run_id)"
                    ),
                    parameters,
                ).one()
            ),
            "drains": tuple(
                connection.execute(
                    text(
                        "SELECT id, target, status, version FROM coordination_runtime_drains "
                        "WHERE task_id = :task_id ORDER BY id"
                    ),
                    parameters,
                ).all()
            ),
        }


def _cleanup(engine, fixture):
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM inbox_messages WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
    _convergence_cleanup(engine, fixture)


def _known_kwargs(fixture, execution, observation, envelope):
    return {
        "tenant_id": fixture.tenant_id,
        "task_id": fixture.task.id,
        "run_id": fixture.run.id,
        "attempt_id": fixture.attempt.id,
        "fencing_token": fixture.attempt.fencing_token,
        "runtime_execution_id": execution.id,
        "observation": observation,
        "received_at": observation.observed_at + timedelta(seconds=1),
        "causation_id": uuid4(),
        "consumer_name": CONSUMER,
        "envelope": envelope,
    }


def _unknown_kwargs(fixture, execution, observation, envelope, *, conflict=None):
    values = _known_kwargs(fixture, execution, observation, envelope)
    if conflict is not None:
        values["conflict"] = conflict
    return values


def test_postgres_terminal_finalization_and_exact_replay_consume_inbox_once():
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _known_observation(execution, now + timedelta(seconds=1))
    kwargs = _known_kwargs(fixture, execution, observation, envelope)
    try:
        service = _terminal_service(fixture, engine)
        first = service.apply_delivery_terminal(**kwargs)
        assert first.kind is CoordinatedKnownTerminalKind.APPLIED
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
        replay = service.apply_delivery_terminal(**kwargs)
        assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
        with Session(engine) as session:
            inbox = session.scalar(
                select(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.consumer_name == CONSUMER,
                )
            )
            execution_record = session.get(RuntimeExecutionRecord, execution.id)
            assert inbox is not None and inbox.message_id == envelope.message_id
            assert execution_record is not None and execution_record.phase == "SUCCEEDED"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_unknown_finalization_and_exact_replay_consume_inbox_once():
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _unknown_observation(execution, now + timedelta(seconds=1))
    kwargs = _unknown_kwargs(fixture, execution, observation, envelope)
    try:
        service = _unknown_service(fixture)
        first = service.park_delivery_unknown(**kwargs)
        assert first.kind is CoordinatedUnknownOutcomeKind.PARKED
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
        replay = service.park_delivery_unknown(**kwargs)
        assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
        with Session(engine) as session:
            execution_record = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_record is not None and execution_record.phase == "OUTCOME_UNKNOWN"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_terminal_same_envelope_concurrency_has_one_projection():
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _known_observation(execution, now + timedelta(seconds=1))
    kwargs = _known_kwargs(fixture, execution, observation, envelope)
    try:
        service = _terminal_service(fixture, engine)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: service.apply_delivery_terminal(**kwargs), (0, 1)
                )
            )
        assert sorted(result.kind.value for result in results) == ["APPLIED", "REPLAY"]
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_inbox_only_and_evidence_only_projections_fail_closed():
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _known_observation(execution, now + timedelta(seconds=1))
    kwargs = _known_kwargs(fixture, execution, observation, envelope)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO inbox_messages "
                    "(tenant_id, consumer_name, message_id, schema_name, "
                    "schema_version, processed_at) VALUES "
                    "(:tenant_id, :consumer, :message_id, :schema, :version, :at)"
                ),
                {
                    "tenant_id": fixture.tenant_id,
                    "consumer": CONSUMER,
                    "message_id": envelope.message_id,
                    "schema": envelope.schema_name,
                    "version": envelope.schema_version,
                    "at": envelope.occurred_at,
                },
            )
        with pytest.raises(RuntimeExecutionConflict, match="no exact terminal evidence"):
            _terminal_service(fixture, engine).apply_delivery_terminal(**kwargs)
        assert _counts(engine, fixture, execution.id) == (0, 1, 0)

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM inbox_messages WHERE tenant_id = :tenant_id"),
                {"tenant_id": fixture.tenant_id},
            )
        direct_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in {"consumer_name", "envelope"}
        }
        direct = _terminal_service(fixture, engine).apply_known_terminal(**direct_kwargs)
        assert direct.kind is CoordinatedKnownTerminalKind.APPLIED
        with pytest.raises(RuntimeExecutionConflict, match="without its delivery Inbox"):
            _terminal_service(fixture, engine).apply_delivery_terminal(**kwargs)
        assert _counts(engine, fixture, execution.id) == (1, 0, 1)
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize("failure", ["inbox", "commit"])
def test_postgres_delivery_finalization_writer_or_commit_failure_rolls_back(failure, monkeypatch):
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _known_observation(execution, now + timedelta(seconds=1))
    kwargs = _known_kwargs(fixture, execution, observation, envelope)
    try:
        before = _business_projection(engine, fixture, execution.id)
        if failure == "inbox":
            def fail_inbox_add(_self, _message):
                raise RuntimeError("injected c2f5e Inbox writer failure")

            monkeypatch.setattr(SqlAlchemyInboxRepository, "add", fail_inbox_add)
        else:
            def fail_commit(_self):
                raise RuntimeError("injected c2f5e commit failure")

            monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)
        expected_error = "Inbox writer failure" if failure == "inbox" else "commit failure"
        with pytest.raises(RuntimeError, match=expected_error):
            _terminal_service(fixture, engine).apply_delivery_terminal(**kwargs)
        assert _counts(engine, fixture, execution.id) == (0, 0, 0)
        assert _business_projection(engine, fixture, execution.id) == before
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_unknown_conflict_pair_is_atomic_replayable_and_missing_row_fails_closed():
    engine = _engine()
    fixture, execution, now = _running_fixture(engine)
    envelope = _envelope(fixture)
    observation = _unknown_observation(execution, now + timedelta(seconds=1))
    candidate = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(uuid4()),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=observation.observed_at,
        provider_event_id=f"c2f5e-conflict-{uuid4().hex}",
        provider_sequence=2,
        output={"contradictory": True},
    )
    conflict = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=observation.observed_at,
    )
    kwargs = _unknown_kwargs(fixture, execution, observation, envelope, conflict=conflict)
    try:
        service = _unknown_service(fixture)
        first = service.park_delivery_unknown(**kwargs)
        assert first.kind is CoordinatedUnknownOutcomeKind.PARKED
        with Session(engine) as session:
            outcomes = sorted(
                record.processing_outcome
                for record in session.scalars(
                    select(RuntimeObservationRecord).where(
                        RuntimeObservationRecord.runtime_execution_id == execution.id
                    )
                ).all()
            )
        assert outcomes == ["APPLIED", "CONFLICT"]
        replay = service.park_delivery_unknown(**kwargs)
        assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
        assert _counts(engine, fixture, execution.id) == (2, 1, 1)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM runtime_observations WHERE runtime_execution_id = :id "
                    "AND processing_outcome = 'CONFLICT'"
                ),
                {"id": execution.id},
            )
        with pytest.raises(RuntimeExecutionConflict, match="contradictory evidence"):
            service.park_delivery_unknown(**kwargs)
        assert _counts(engine, fixture, execution.id) == (1, 1, 1)
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
