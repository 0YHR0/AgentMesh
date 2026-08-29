"""PostgreSQL transaction and inheritance checks for authority cohorts."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.authority_cohorts import AuthorityCohortResolver, ContinuationKind
from agentmesh.config import get_settings
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.tasks import RunRole
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    OutboxEventRecord,
    RuntimeVersionRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk.builtin import builtin_langgraph_version_id

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL authority cohort tests",
    ),
]


def _fixture(session: Session) -> tuple[str, object, object]:
    """Create a managed parent against the seeded platform LangGraph v2."""
    tenant_id = f"cohort-pg-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    version_id = builtin_langgraph_version_id("v2")
    version = session.get(RuntimeVersionRecord, version_id)
    if version is None:
        pytest.skip("built-in LangGraph v2 is not seeded in this PostgreSQL database")
    task_id, parent_id = uuid4(), uuid4()
    session.add(
        TaskRecord(
            id=task_id,
            tenant_id=tenant_id,
            project_id="cohort-integration",
            objective="authority cohort integration",
            input={},
            status="READY",
            current_run_id=parent_id,
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
    parent = TaskRunRecord(
        id=parent_id,
        task_id=task_id,
        thread_id=str(parent_id),
        agent_id="cohort-parent",
        agent_version_id=None,
        agent_version_digest=None,
        runtime_version_id=version_id,
        runtime_execution_id=None,
        runtime_execution_intent_id=uuid4(),
        runtime_authority="managed",
        comparison_mode="off",
        role="EXECUTOR",
        revision_number=0,
        subtask_id=None,
        status="SUCCEEDED",
        output={"ok": True},
        error=None,
        queued_at=now,
        started_at=now,
        completed_at=now,
        pause_requested_at=None,
        paused_at=None,
        resumed_at=None,
        paused_from_status=None,
    )
    session.add(parent)
    session.flush()
    return tenant_id, task_id, parent_id


def _cleanup(engine, tenant_id: str) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM outbox_events WHERE tenant_id = :tenant", {"tenant": tenant_id}
        )
        connection.exec_driver_sql(
            "DELETE FROM tasks WHERE tenant_id = :tenant", {"tenant": tenant_id}
        )


def test_postgres_cohort_continuation_and_run_requested_rollback() -> None:
    engine = create_engine(get_settings().database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_id = None
    try:
        with factory() as session, session.begin():
            tenant_id, task_id, parent_id = _fixture(session)
        resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("full"))
        uow_factory = SqlAlchemyUnitOfWorkFactory(factory)
        with pytest.raises(RuntimeError, match="rollback cohort"):
            with uow_factory() as uow:
                task = uow.tasks.get(task_id, for_update=True)
                parent = uow.runs.get(parent_id, for_update=True)
                assert task is not None and parent is not None
                child = resolver.create_continuation_in_uow(
                    uow,
                    task,
                    agent_id="cohort-child",
                    agent_version_id=None,
                    agent_version_digest=None,
                    role=RunRole.REVIEWER,
                    revision_number=0,
                    parent_run=parent,
                    kind=ContinuationKind.REVIEWER,
                )
                uow.runs.add(child)
                uow.outbox.add(
                    MessageEnvelope.run_requested(
                        tenant_id=tenant_id, task_id=task_id, run_id=child.id
                    )
                )
                uow.flush()
                raise RuntimeError("rollback cohort")
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(TaskRunRecord)
                    .where(TaskRunRecord.id == child.id)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventRecord)
                    .where(OutboxEventRecord.tenant_id == tenant_id)
                )
                == 0
            )
            parent_record = session.get(TaskRunRecord, parent_id)
            assert parent_record is not None
            assert parent_record.runtime_authority == "managed"
            assert parent_record.runtime_version_id == builtin_langgraph_version_id("v2")

        _cleanup(engine, tenant_id)
        tenant_id = None
        with factory() as session, session.begin():
            tenant_id, task_id, parent_id = _fixture(session)
        with uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            parent = uow.runs.get(parent_id, for_update=True)
            assert task is not None and parent is not None
            child = resolver.create_continuation_in_uow(
                uow,
                task,
                agent_id="cohort-child-success",
                agent_version_id=None,
                agent_version_digest=None,
                role=RunRole.REVIEWER,
                revision_number=0,
                parent_run=parent,
                kind=ContinuationKind.REVIEWER,
            )
            assert child.runtime_authority == "managed"
            assert child.runtime_version_id == parent.runtime_version_id
            assert child.runtime_execution_intent_id != parent.runtime_execution_intent_id
            uow.runs.add(child)
            uow.commit()
        with factory() as session:
            assert session.get(TaskRunRecord, child.id) is not None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventRecord)
                    .where(OutboxEventRecord.tenant_id == tenant_id)
                )
                == 0
            )
    finally:
        if tenant_id is not None:
            _cleanup(engine, tenant_id)
        engine.dispose()
