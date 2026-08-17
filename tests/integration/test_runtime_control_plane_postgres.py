"""PostgreSQL evidence for the A1 runtime registry persistence boundary.

These tests intentionally use the migrated PostgreSQL service from CI.  The
domain tests cover the transition matrix; this module verifies the database
constraints and idempotent bootstrap against the real engine.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
    RuntimeRegistration,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
    RuntimeVisibility,
)
from agentmesh.infrastructure.postgres.models import (
    PrincipalRecord,
    RuntimeExecutionRecord,
    RuntimeRegistrationRecord,
    RuntimeVersionRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.runtime_repositories import SqlAlchemyRuntimeRepository

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def _descriptor() -> dict[str, object]:
    return {
        "schema_name": "agentmesh.runtime-descriptor",
        "schema_version": 1,
        "runtime_key": "agentmesh.test.postgres",
        "display_name": "PostgreSQL test runtime",
        "adapter_kind": "python-in-process",
        "capabilities": {
            "execution_mode": ["inline", "managed_async"],
            "reattach": True,
            "cancel": "cooperative",
            "pause_resume": True,
            "checkpoint": True,
            "fork": False,
            "event_stream": True,
            "tool_bridge": [],
            "artifact_io": ["reference"],
            "isolation_profiles": ["trusted-in-process"],
            "modalities": ["text"],
        },
        "limits": {
            "max_assignment_bytes": 262144,
            "max_event_bytes": 65536,
            "max_result_bytes": 262144,
            "max_artifact_refs": 128,
        },
    }


def _fixture(session: Session) -> tuple[SqlAlchemyRuntimeRepository, RuntimeExecution]:
    """Create the minimum real Task -> Run -> Runtime authority chain."""
    tenant = f"runtime-pg-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    principal_id = uuid4()
    session.add(
        PrincipalRecord(
            id=principal_id,
            tenant_id=tenant,
            principal_type="SERVICE",
            status="ACTIVE",
            display_name="runtime integration owner",
            created_at=now,
            updated_at=now,
            revision=1,
        )
    )
    task_id, run_id = uuid4(), uuid4()
    session.add(
        TaskRecord(
            id=task_id,
            tenant_id=tenant,
            project_id="integration",
            objective="runtime integration task",
            input={},
            status="QUEUED",
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
    registration = RuntimeRegistration.create(
        name=f"postgres-{uuid4().hex}",
        owner_principal_id=principal_id,
        visibility=RuntimeVisibility.PLATFORM,
        now=now,
    )
    version = RuntimeVersion(
        id=uuid4(),
        runtime_id=registration.id,
        api_version=1,
        adapter_kind="python-in-process",
        artifact_digest="a" * 64,
        configuration_digest="b" * 64,
        descriptor=_descriptor(),
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility={},
        status=RuntimeVersionStatus.PUBLISHED,
        created_at=now,
        published_at=now,
    )
    session.add_all(
        [
            RuntimeRegistrationRecord(**{
                "id": registration.id,
                "tenant_id": None,
                "name": registration.name,
                "owner_principal_id": principal_id,
                "visibility": "platform",
                "status": "ACTIVE",
                "default_version_id": version.id,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }),
            RuntimeVersionRecord(**{
                "id": version.id,
                "runtime_id": registration.id,
                "api_version": 1,
                "adapter_kind": version.adapter_kind,
                "artifact_digest": version.artifact_digest,
                "configuration_digest": version.configuration_digest,
                "descriptor": _descriptor(),
                "trust_profile": "built_in",
                "compatibility": {},
                "status": "PUBLISHED",
                "created_at": now,
                "published_at": now,
                "revoked_at": None,
            }),
            TaskRunRecord(
                id=run_id,
                task_id=task_id,
                thread_id=str(run_id),
                agent_id="integration-agent",
                agent_version_id=None,
                agent_version_digest=None,
                runtime_version_id=version.id,
                runtime_execution_id=None,
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
        tenant_id=tenant,
        run_id=run_id,
        runtime_version_id=version.id,
        assignment_id=uuid4(),
        assignment_digest="c" * 64,
        dispatch_key=f"runtime-dispatch:{tenant}:{uuid4()}",
        dispatch_digest="d" * 64,
        now=now,
    )
    repository = SqlAlchemyRuntimeRepository(session)
    repository.add_execution(execution)
    session.flush()
    return repository, execution


def test_runtime_schema_has_a1_constraints_and_indexes() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        database = inspect(engine)
        checks = {
            constraint["name"]
            for table in (
                "runtime_observations",
                "runtime_lifecycle_operations",
            )
            for constraint in database.get_check_constraints(table)
        }
        assert {
            "ck_runtime_observations_digests",
            "ck_runtime_observations_phase",
            "ck_runtime_lifecycle_digest",
        } <= checks
        indexes = {
            index["name"]
            for index in database.get_indexes("task_runs")
        }
        assert {
            "ix_task_runs_runtime_execution",
            "ix_task_runs_runtime_version",
        } <= indexes
    finally:
        engine.dispose()


def test_builtin_runtime_seed_is_deterministic_and_idempotent() -> None:
    settings = get_settings()
    seed_builtin_registry(settings)
    seed_builtin_registry(settings)
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, api_version, descriptor->>'runtime_key' AS runtime_key "
                    "FROM runtime_versions "
                    "WHERE runtime_id = 'c5b0d15a-33ef-57dc-92d6-c685bcd31470'"
                )
            ).all()
        assert len(rows) == 1
        assert str(rows[0].id) == "5c6ffbe8-2226-5fbc-bddf-08388949e82e"
        assert rows[0].api_version == 1
        assert rows[0].runtime_key == "agentmesh.langgraph"
    finally:
        engine.dispose()


def test_dispatch_digest_conflict_and_one_active_execution_are_database_enforced() -> None:
    engine = create_engine(get_settings().database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with factory() as session:
            repository, execution = _fixture(session)
            assert repository.get_execution_by_dispatch(
                execution.dispatch_key, tenant_id=execution.tenant_id
            ).id == execution.id
            duplicate = RuntimeExecution.prepare(
                tenant_id=execution.tenant_id,
                run_id=execution.run_id,
                runtime_version_id=execution.runtime_version_id,
                assignment_id=execution.assignment_id,
                assignment_digest="e" * 64,
                dispatch_key=execution.dispatch_key,
                dispatch_digest="f" * 64,
            )
            repository.add_execution(duplicate)
            with pytest.raises(IntegrityError):
                session.flush()
    finally:
        engine.dispose()


def test_concurrent_prepare_for_one_run_allows_only_one_active_execution() -> None:
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with factory() as session:
            _, template = _fixture(session)
            session.query(RuntimeExecutionRecord).filter_by(id=template.id).update(
                {
                    "phase": RuntimeExecutionPhase.SUCCEEDED.value,
                    "terminal_at": datetime.now(timezone.utc),
                }
            )
            session.commit()

        barrier = Barrier(2)

        def insert_candidate(suffix: str) -> bool:
            with factory() as session:
                candidate = RuntimeExecution.prepare(
                    tenant_id=template.tenant_id,
                    run_id=template.run_id,
                    runtime_version_id=template.runtime_version_id,
                    assignment_id=template.assignment_id,
                    assignment_digest=template.assignment_digest,
                    dispatch_key=f"{template.dispatch_key}-{suffix}",
                    dispatch_digest="e" * 64,
                )
                session.add(
                    RuntimeExecutionRecord(
                        id=candidate.id,
                        tenant_id=candidate.tenant_id,
                        run_id=candidate.run_id,
                        runtime_version_id=candidate.runtime_version_id,
                        assignment_id=candidate.assignment_id,
                        assignment_digest=candidate.assignment_digest,
                        dispatch_key=candidate.dispatch_key,
                        dispatch_digest=candidate.dispatch_digest,
                        provider_execution_ref=None,
                        provider_generation=None,
                        phase=candidate.phase.value,
                        current_owner_attempt_id=None,
                        current_fencing_token=None,
                        provider_sequence=None,
                        checkpoint_ref=None,
                        workspace_ref=None,
                        version=1,
                        created_at=candidate.created_at,
                        updated_at=candidate.updated_at,
                        terminal_at=None,
                    )
                )
                barrier.wait()
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    return False
                return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(insert_candidate, ("a", "b")))
        assert sum(results) == 1
    finally:
        engine.dispose()


def test_outcome_unknown_is_returned_as_an_unresolved_blocker() -> None:
    engine = create_engine(get_settings().database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with factory() as session:
            repository, execution = _fixture(session)
            session.query(RuntimeExecutionRecord).filter_by(id=execution.id).update(
                {
                    "phase": RuntimeExecutionPhase.OUTCOME_UNKNOWN.value,
                    "terminal_at": datetime.now(timezone.utc),
                }
            )
            session.flush()
            unresolved = repository.get_active_or_unresolved_for_run(
                execution.run_id, tenant_id=execution.tenant_id
            )
            assert unresolved is not None
            assert unresolved.phase is RuntimeExecutionPhase.OUTCOME_UNKNOWN
    finally:
        engine.dispose()


def test_owner_fencing_cas_and_verified_replacement_preserve_history() -> None:
    engine = create_engine(get_settings().database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    now = datetime.now(timezone.utc)
    try:
        with factory() as session:
            repository, execution = _fixture(session)
            first_attempt, replacement_attempt = uuid4(), uuid4()
            session.add_all(
                [
                    TaskAttemptRecord(
                        id=first_attempt,
                        run_id=execution.run_id,
                        trace_id=uuid4().hex,
                        worker_id="worker-a",
                        lease_token=uuid4(),
                        fencing_token=1,
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
                    ),
                    TaskAttemptRecord(
                        id=replacement_attempt,
                        run_id=execution.run_id,
                        trace_id=uuid4().hex,
                        worker_id="worker-b",
                        lease_token=uuid4(),
                        fencing_token=2,
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
                    ),
                ]
            )
            session.flush()
            claimed = repository.claim_execution_owner(
                execution_id=execution.id,
                tenant_id=execution.tenant_id,
                attempt_id=first_attempt,
                fencing_token=1,
                expected_owner_attempt_id=None,
                expected_fencing_token=None,
                expected_version=1,
                now=now,
                claim_reason="initial",
            )
            with pytest.raises(InvalidTaskTransition):
                repository.claim_execution_owner(
                    execution_id=execution.id,
                    tenant_id=execution.tenant_id,
                    attempt_id=replacement_attempt,
                    fencing_token=2,
                    expected_owner_attempt_id=None,
                    expected_fencing_token=None,
                    expected_version=claimed.version,
                    now=now,
                    claim_reason="replacement",
                )
            observed = claimed.apply_observation(
                phase=RuntimeExecutionPhase.DISPATCHING,
                provider_sequence=1,
                provider_execution_ref="opaque-provider-handle",
                now=now,
            )
            repository.save_execution(observed, tenant_id=execution.tenant_id)
            old_record = session.get(TaskAttemptRecord, first_attempt)
            old_record.lease_expires_at = now - timedelta(seconds=1)
            session.flush()
            replaced = repository.claim_execution_owner(
                execution_id=execution.id,
                tenant_id=execution.tenant_id,
                attempt_id=replacement_attempt,
                fencing_token=2,
                expected_owner_attempt_id=first_attempt,
                expected_fencing_token=1,
                expected_version=observed.version,
                now=now,
                claim_reason="replacement",
                reattach_evidence=ReattachEvidence(
                    execution_id=execution.id,
                    assignment_digest=execution.assignment_digest,
                    provider_execution_ref="opaque-provider-handle",
                    inspected_at=now,
                ),
            )
            assert replaced.current_owner_attempt_id == replacement_attempt
            history = list(
                session.scalars(
                    text(
                        "SELECT * FROM runtime_ownership_history "
                        "WHERE runtime_execution_id = :execution_id"
                    ).bindparams(execution_id=execution.id)
                )
            )
            assert len(history) == 2
            session.commit()
    finally:
        engine.dispose()


def test_observation_duplicate_conflict_gap_and_stale_evidence_are_retained() -> None:
    engine = create_engine(get_settings().database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    now = datetime.now(timezone.utc)
    try:
        with factory() as session:
            repository, execution = _fixture(session)
            for index, outcome in enumerate(
                (
                    RuntimeObservationOutcome.DUPLICATE,
                    RuntimeObservationOutcome.CONFLICT,
                    RuntimeObservationOutcome.GAP,
                    RuntimeObservationOutcome.STALE_OWNER,
                )
            ):
                repository.add_observation(
                    RuntimeObservationEvidence(
                        id=uuid4(),
                        tenant_id=execution.tenant_id,
                        runtime_execution_id=execution.id,
                        observation_id="same-provider-event",
                        observation_digest="a" * 64,
                        assignment_id=execution.assignment_id,
                        assignment_digest=execution.assignment_digest,
                        provider_sequence=index,
                        phase=RuntimeExecutionPhase.RUNNING,
                        observed_at=now,
                        received_at=now,
                        safe_summary=None,
                        processing_outcome=outcome,
                        provider_event_present=False,
                    )
                )
            session.flush()
            records = repository.prior_observations(
                execution.id,
                tenant_id=execution.tenant_id,
                observation_id="same-provider-event",
                digest="a" * 64,
            )
            assert [record.processing_outcome for record in records] == [
                RuntimeObservationOutcome.DUPLICATE,
                RuntimeObservationOutcome.CONFLICT,
                RuntimeObservationOutcome.GAP,
                RuntimeObservationOutcome.STALE_OWNER,
            ]
    finally:
        engine.dispose()
