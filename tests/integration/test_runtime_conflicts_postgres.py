"""PostgreSQL service coverage for the forced safe conflict writer."""

from __future__ import annotations

import os
from builtins import RuntimeError as BuiltinRuntimeError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.messaging import InboxMessage, MessageEnvelope
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    InboxMessageRecord,
    OutboxEventRecord,
    RuntimeExecutionRecord,
    RuntimeObservationRecord,
    TaskAttemptRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
)
from tests.integration.test_runtime_control_plane_postgres import _fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL tests",
    ),
]


def test_postgres_conflict_writer_is_locked_and_exactly_replayable() -> None:
    from agentmesh.config import get_settings

    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            repository, execution = _fixture(session)
            now = datetime.now(timezone.utc)
            attempt_id = uuid4()
            session.add(
                TaskAttemptRecord(
                    id=attempt_id,
                    run_id=execution.run_id,
                    trace_id=uuid4().hex,
                    worker_id="conflict-writer",
                    lease_token=uuid4(),
                    fencing_token=5,
                    status="RUNNING",
                    lease_expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                    started_at=now,
                    completed_at=None,
                    error=None,
                    reserved_tokens=0,
                    reserved_cost_micros=0,
                    settled_tokens=None,
                    settled_cost_micros=None,
                    budget_settlement_source=None,
                )
            )
            session.flush()
            execution = repository.claim_execution_owner(
                execution_id=execution.id,
                tenant_id=execution.tenant_id,
                attempt_id=attempt_id,
                fencing_token=5,
                expected_owner_attempt_id=None,
                expected_fencing_token=None,
                expected_version=execution.version,
                now=now,
                claim_reason="initial",
            )
            uow = SimpleNamespace(runtimes=repository)
            service = RuntimeRegistryService(
                uow_factory=lambda: uow,
                tenant_id=execution.tenant_id,
                feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
            )

            def envelope(provider_event_id: str):
                candidate = RuntimeObservation(
                    observation_id=str(uuid4()),
                    runtime_execution_id=str(uuid4()),
                    assignment_id=str(uuid4()),
                    assignment_digest="c" * 64,
                    phase=RuntimePhase.FAILED,
                    observed_at=now,
                    provider_event_id=provider_event_id,
                )
                return build_managed_runtime_conflict_observation(
                    candidate,
                    expected_execution_id=execution.id,
                    expected_assignment_id=execution.assignment_id,
                    expected_assignment_digest=execution.assignment_digest,
                    fallback_observed_at=now,
                )

            first_envelope = envelope("provider-a")
            first = service.record_conflicting_observation_in_uow(
                uow,
                execution_id=execution.id,
                attempt_id=attempt_id,
                fencing_token=5,
                observation=first_envelope,
                now=now,
            )
            replay = service.record_conflicting_observation_in_uow(
                uow,
                execution_id=execution.id,
                attempt_id=attempt_id,
                fencing_token=5,
                observation=first_envelope,
                now=now,
            )
            second = service.record_conflicting_observation_in_uow(
                uow,
                execution_id=execution.id,
                attempt_id=attempt_id,
                fencing_token=5,
                observation=envelope("provider-b"),
                now=now,
            )
            session.flush()
            assert replay == first
            assert second.observation_digest != first.observation_digest
            assert (
                repository.get_execution(execution.id, tenant_id=execution.tenant_id).phase
                is RuntimeExecutionPhase.PREPARED
            )
            assert (
                session.scalar(
                    select(func.count(RuntimeObservationRecord.id)).where(
                        RuntimeObservationRecord.runtime_execution_id == execution.id
                    )
                )
                == 2
            )
            session.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("stage", ["after_conflict", "after_synthetic", "after_messaging"])
def test_postgres_conflict_and_synthetic_writes_rollback_as_one_transaction(
    stage: str,
) -> None:
    from agentmesh.config import get_settings

    engine = create_engine(get_settings().database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    try:
        with Session(engine) as session:
            repository, execution = _fixture(session)
            now = datetime.now(timezone.utc)
            attempt_id = uuid4()
            session.add(
                TaskAttemptRecord(
                    id=attempt_id,
                    run_id=execution.run_id,
                    trace_id=uuid4().hex,
                    worker_id="conflict-rollback",
                    lease_token=uuid4(),
                    fencing_token=5,
                    status="RUNNING",
                    lease_expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                    started_at=now,
                    completed_at=None,
                    error=None,
                    reserved_tokens=0,
                    reserved_cost_micros=0,
                    settled_tokens=None,
                    settled_cost_micros=None,
                    budget_settlement_source=None,
                )
            )
            session.flush()
            execution = repository.claim_execution_owner(
                execution_id=execution.id,
                tenant_id=execution.tenant_id,
                attempt_id=attempt_id,
                fencing_token=5,
                expected_owner_attempt_id=None,
                expected_fencing_token=None,
                expected_version=execution.version,
                now=now,
                claim_reason="initial",
            )
            session.commit()
            candidate = RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(uuid4()),
                assignment_id=str(uuid4()),
                assignment_digest="e" * 64,
                phase=RuntimePhase.FAILED,
                observed_at=now,
                provider_event_id="rollback-provider",
                usage={"must-not-be-retained": 1},
            )
            conflict = build_managed_runtime_conflict_observation(
                candidate,
                expected_execution_id=execution.id,
                expected_assignment_id=execution.assignment_id,
                expected_assignment_digest=execution.assignment_digest,
                fallback_observed_at=now,
            )
            synthetic = RuntimeObservation(
                observation_id=str(uuid5(NAMESPACE_URL, f"{execution.id}:rollback-unknown")),
                runtime_execution_id=str(execution.id),
                assignment_id=str(execution.assignment_id),
                assignment_digest=execution.assignment_digest,
                phase=RuntimePhase.OUTCOME_UNKNOWN,
                observed_at=now,
                provider_event_id="rollback-unknown",
                error=RuntimeError(
                    code="runtime.terminal_contract_invalid",
                    category=ErrorCategory.UNKNOWN,
                    message="rollback",
                    retry_disposition=RetryDisposition.RECONCILE,
                ),
            )
        with pytest.raises(BuiltinRuntimeError, match="force rollback"):
            with factory() as uow:
                service = RuntimeRegistryService(
                    uow_factory=lambda: uow,
                    tenant_id=execution.tenant_id,
                    feature_gates=FeatureGateSet.from_config(
                        "full", "managed_agent_runtime=true"
                    ),
                )
                execution = uow.runtimes.get_execution(
                    execution.id, tenant_id=execution.tenant_id, for_update=True
                )
                assert execution is not None
                service.record_conflicting_observation_in_uow(
                    uow,
                    execution_id=execution.id,
                    attempt_id=attempt_id,
                    fencing_token=5,
                    observation=conflict,
                    now=now,
                )
                if stage == "after_conflict":
                    raise BuiltinRuntimeError("force rollback")
                service.record_observation_in_uow(
                    uow,
                    execution_id=execution.id,
                    observation_id=synthetic.observation_id,
                    observation_digest="f" * 64,
                    assignment_id=execution.assignment_id,
                    assignment_digest=execution.assignment_digest,
                    phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN,
                    provider_sequence=None,
                    observed_at=synthetic.observed_at,
                    evidence={"provider_event_id": synthetic.provider_event_id},
                    safe_summary="rollback",
                    attempt_id=attempt_id,
                    fencing_token=5,
                    now=now,
                )
                if stage == "after_synthetic":
                    raise BuiltinRuntimeError("force rollback")
                event = MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.reconciliation.required",
                    tenant_id=execution.tenant_id,
                    aggregate_id=execution.run_id,
                    payload={"runtime_execution_id": str(execution.id)},
                )
                uow.outbox.add(event)
                uow.inbox.add(InboxMessage.processed("rollback-test", event))
                uow.flush()
                raise BuiltinRuntimeError("force rollback")

        with Session(engine) as verify:
            assert verify.scalar(
                select(func.count(RuntimeObservationRecord.id)).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id
                )
            ) == 0
            assert verify.get(RuntimeExecutionRecord, execution.id).phase == "PREPARED"
            assert verify.scalar(
                select(func.count(InboxMessageRecord.id)).where(
                    InboxMessageRecord.tenant_id == execution.tenant_id
                )
            ) == 0
            assert verify.scalar(
                select(func.count(OutboxEventRecord.id)).where(
                    OutboxEventRecord.tenant_id == execution.tenant_id
                )
            ) == 0
    finally:
        engine.dispose()
