"""Task transitions and notification delivery share one durable PostgreSQL boundary."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.config import get_settings
from agentmesh.domain.policy import GovernedAction, GovernedActionType, PolicyResult
from agentmesh.domain.tasks import Task, TaskStatus
from agentmesh.infrastructure.postgres.models import FeishuNotificationRecord
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.integrations.feishu_notifications import (
    FeishuNotificationStore,
    FeishuNotificationWorker,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
    ),
]


def test_opt_in_transition_is_atomic_and_delivery_is_independent() -> None:
    tenant_id = f"feishu-{uuid4().hex}"
    engine = create_engine(get_settings().database_url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    enabled = SqlAlchemyUnitOfWorkFactory(sessions, feishu_notifications_enabled=True)
    disabled = SqlAlchemyUnitOfWorkFactory(sessions)
    task = Task.create(tenant_id=tenant_id, objective="Sample report")
    second = Task.create(tenant_id=tenant_id, objective="No external notifications")
    rolled_back = Task.create(tenant_id=tenant_id, objective="Rolled-back transition")
    try:
        with enabled() as uow:
            uow.tasks.add(task)
            uow.commit()
        with disabled() as uow:
            uow.tasks.add(second)
            uow.commit()
        with enabled() as uow:
            uow.tasks.add(rolled_back)
            uow.commit()
        with enabled() as uow:
            changed = uow.tasks.get(rolled_back.id)
            assert changed is not None
            changed.status = TaskStatus.FAILED
            changed.version += 1
            changed.updated_at = datetime.now(timezone.utc)
            uow.tasks.save(changed)
            uow.rollback()
        with enabled() as uow:
            changed = uow.tasks.get(task.id)
            assert changed is not None
            changed.status = TaskStatus.COMPLETED
            changed.output = {"summary": "Finished"}
            changed.version += 1
            changed.updated_at = datetime.now(timezone.utc)
            uow.tasks.save(changed)
            uow.commit()
        with disabled() as uow:
            changed = uow.tasks.get(second.id)
            assert changed is not None
            changed.status = TaskStatus.FAILED
            changed.error = "test"
            changed.version += 1
            changed.updated_at = datetime.now(timezone.utc)
            uow.tasks.save(changed)
            uow.commit()
        with sessions() as session:
            jobs = list(
                session.scalars(
                    select(FeishuNotificationRecord).where(
                        FeishuNotificationRecord.tenant_id == tenant_id
                    )
                )
            )
        assert len(jobs) == 1
        assert jobs[0].subject_type == "TASK"
        assert jobs[0].subject_id == task.id
        assert jobs[0].event_kind == "COMPLETED"

        class FakeClient:
            sent = []

            def send(self, *, notification_id, card):
                self.sent.append((notification_id, card))

        client = FakeClient()
        worker = FeishuNotificationWorker(
            worker_id="feishu-test",
            store=FeishuNotificationStore(sessions, tenant_id=tenant_id),
            client=client,
            task_base_url=None,
            include_content=False,
        )
        assert worker.run_once() == 1
        assert worker.run_once() == 0
        assert len(client.sent) == 1
        with sessions() as session:
            job = session.get(FeishuNotificationRecord, jobs[0].id)
            assert job is not None and job.status == "DELIVERED"
    finally:
        engine.dispose()


def test_governed_approval_creates_a_separate_notification_job() -> None:
    tenant_id = f"feishu-policy-{uuid4().hex}"
    engine = create_engine(get_settings().database_url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    factory = SqlAlchemyUnitOfWorkFactory(sessions, feishu_notifications_enabled=True)
    now = datetime.now(timezone.utc)
    action = GovernedAction.create(
        tenant_id=tenant_id,
        requester_id="operator",
        action_type=GovernedActionType.MCP_TOOL_INVOKE,
        resource_type="tool",
        resource_id=uuid4(),
        arguments={"private": "do not notify"},
        policy_result=PolicyResult.REQUIRE_APPROVAL,
        reason_code="test",
        policy_bundle="test",
        policy_version="1",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    try:
        with factory() as uow:
            uow.policy.add_action(action)
            uow.commit()
        with sessions() as session:
            job = session.scalars(
                select(FeishuNotificationRecord).where(
                    FeishuNotificationRecord.tenant_id == tenant_id
                )
            ).one()
            assert job.subject_type == "GOVERNED_ACTION"
            assert job.subject_id == action.id
            assert job.event_kind == "PENDING_APPROVAL"
    finally:
        engine.dispose()
