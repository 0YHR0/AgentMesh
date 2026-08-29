"""PostgreSQL service coverage for the forced safe conflict writer."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import RuntimeObservationRecord
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
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
            execution = repository.claim_execution_owner(
                execution_id=execution.id,
                tenant_id=execution.tenant_id,
                attempt_id=attempt_id,
                fencing_token=5,
                expected_owner_attempt_id=None,
                expected_fencing_token=None,
                expected_version=execution.version,
                now=now,
                claim_reason="integration-conflict",
            )
            uow = SimpleNamespace(runtimes=repository)
            service = RuntimeRegistryService(
                uow_factory=lambda: uow,
                tenant_id=execution.tenant_id,
                feature_gates=FeatureGateSet.from_config(
                    "full", "managed_agent_runtime=true"
                ),
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
            assert repository.get_execution(
                execution.id, tenant_id=execution.tenant_id
            ).phase is RuntimeExecutionPhase.PREPARED
            assert session.scalar(
                select(func.count(RuntimeObservationRecord.id)).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id
                )
            ) == 2
            session.rollback()
    finally:
        engine.dispose()
