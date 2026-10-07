"""Task transitions and notification delivery share one durable PostgreSQL boundary."""

import os
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.config import get_settings
from agentmesh.domain.coordination import Subtask
from agentmesh.domain.policy import GovernedAction, GovernedActionType, PolicyResult
from agentmesh.domain.tasks import RunRole, RunStatus, Task, TaskExecutionMode, TaskRun, TaskStatus
from agentmesh.infrastructure.postgres.models import FeishuNotificationRecord, TaskRecord
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


@pytest.fixture
def collaboration_sessions():
    engine = create_engine(get_settings().database_url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        yield sessions
    finally:
        engine.dispose()


def _run_fixture(sessions, *, role=RunRole.EXECUTOR, mode=TaskExecutionMode.COORDINATED):
    task = Task.create(
        tenant_id=f"feishu-collaboration-{uuid4().hex}",
        objective="Collaborate without exposing private work input",
        execution_mode=mode,
        plan_version=1 if mode is TaskExecutionMode.COORDINATED else None,
        plan_digest=f"sha256:{'a' * 64}" if mode is TaskExecutionMode.COORDINATED else None,
    )
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="analysis",
        objective="Analyze supplied input",
        input={},
        required_capabilities=(),
        preferred_agent_id=None,
        initially_ready=True,
    )
    factory = SqlAlchemyUnitOfWorkFactory(sessions)
    with factory() as uow:
        uow.tasks.add(task)
        uow.flush()
        uow.subtasks.add(subtask)
        uow.commit()
    run = TaskRun.request(task.id, "collaboration-test-agent", role=role, subtask_id=subtask.id)
    return task, subtask, run


def _snapshot():
    return {
        "schema_version": 1,
        "work_item": {"objective": "Bound input", "input": {"private": "do not queue this"}},
        "transfers": [],
    }


def _jobs(sessions, task):
    with sessions() as session:
        return list(
            session.scalars(
                select(FeishuNotificationRecord).where(
                    FeishuNotificationRecord.tenant_id == task.tenant_id
                )
            )
        )


def _enabled(sessions):
    return SqlAlchemyUnitOfWorkFactory(
        sessions, feishu_notifications_enabled=True, feishu_collaboration_enabled=True
    )


def test_real_store_delivers_historical_start_and_succeeded_result(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.start()
    run.pin_work_item_snapshot(_snapshot())
    run.succeed({"summary": "Verified public work summary", "private": "not for chat"})
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    with sessions() as session, session.begin():
        session.get(TaskRecord, task.id).status = "COMPLETED"

    class Client:
        sent = []

        def send(self, **kwargs):
            self.sent.append(kwargs)

    client = Client()
    worker = FeishuNotificationWorker(
        worker_id="test", store=FeishuNotificationStore(sessions, tenant_id=task.tenant_id),
        client=client, task_base_url=None, include_content=True, sync_collaboration=True,
        send_interval_seconds=0,
    )
    assert worker.run_once() == 2
    assert worker.run_once() == 0
    assert len(client.sent) == 2
    rendered = str(client.sent)
    assert "Verified public work summary" in rendered
    assert "not for chat" not in rendered and "do not queue this" not in rendered
    assert {value.status for value in _jobs(sessions, task)} == {"DELIVERED"}


def test_collaboration_pin_and_terminal_notification_are_atomic_and_deduplicated(
    collaboration_sessions,
):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    factory = _enabled(sessions)
    with factory() as uow:
        uow.runs.add(run)
        uow.commit()
    run.start()
    run.pin_work_item_snapshot(_snapshot())
    with factory() as uow:
        uow.runs.save(run)
        uow.rollback()
    assert _jobs(sessions, task) == []
    with factory() as uow:
        persisted = uow.runs.get(run.id)
        assert persisted.work_item_snapshot is None
        assert persisted.status is RunStatus.QUEUED
        uow.runs.save(run)
        uow.commit()
    run.succeed({"summary": "Done", "private": "do not queue output"})
    with factory() as uow:
        uow.runs.save(run)
        uow.runs.save(run)
        uow.commit()
    with factory() as uow:
        uow.runs.save(run)
        uow.commit()
    jobs = _jobs(sessions, task)
    assert {job.event_kind for job in jobs} == {"COLLAB_STARTED", "COLLAB_RESULT"}
    assert len(jobs) == 2
    for job in jobs:
        assert job.subject_type == "TASK_RUN"
        assert job.subject_id == run.id
        assert job.subject_revision == 1
        assert job.id == uuid5(NAMESPACE_URL, f"agentmesh:feishu:run:{run.id}:{job.event_kind}")
        assert job.status == "PENDING"


def test_adding_pinned_terminal_run_creates_both_notices(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.start()
    run.pin_work_item_snapshot(_snapshot())
    run.succeed({"summary": "Done"})
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert {job.event_kind for job in _jobs(sessions, task)} == {"COLLAB_STARTED", "COLLAB_RESULT"}


@pytest.mark.parametrize("terminal", [RunStatus.FAILED, RunStatus.CANCELED])
def test_pinned_failure_or_cancellation_creates_one_failure_notice(
    collaboration_sessions, terminal
):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.start()
    run.pin_work_item_snapshot(_snapshot())
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    if terminal is RunStatus.FAILED:
        run.fail("Private failure details stay in the execution record")
    else:
        run.cancel()
    with _enabled(sessions)() as uow:
        uow.runs.save(run)
        uow.runs.save(run)
        uow.commit()
    assert {job.event_kind for job in _jobs(sessions, task)} == {"COLLAB_STARTED", "COLLAB_FAILED"}


@pytest.mark.parametrize("parent,collaboration", [(False, False), (False, True), (True, False)])
def test_collaboration_requires_both_parent_and_child_opt_in(
    collaboration_sessions,
    parent,
    collaboration,
):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.pin_work_item_snapshot(_snapshot())
    factory = SqlAlchemyUnitOfWorkFactory(
        sessions, feishu_notifications_enabled=parent, feishu_collaboration_enabled=collaboration
    )
    with factory() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []


@pytest.mark.parametrize("role", [RunRole.SUPERVISOR, RunRole.REVIEWER])
def test_only_executor_work_items_create_collaboration_notices(collaboration_sessions, role):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions, role=role)
    run.pin_work_item_snapshot(_snapshot())
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []


@pytest.mark.parametrize("terminal", [RunStatus.SUCCEEDED, RunStatus.CANCELED])
def test_unpinned_terminal_run_does_not_claim_actual_collaboration(
    collaboration_sessions, terminal
):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.status = terminal
    run.completed_at = datetime.now(timezone.utc)
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []


@pytest.mark.parametrize(
    "bad",
    [
        "boolean_version",
        "missing_transfers",
        "invalid_work_item",
        "empty_objective",
        "invalid_input",
    ],
)
def test_malformed_snapshot_never_creates_collaboration_notices(collaboration_sessions, bad):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    snapshot = _snapshot()
    if bad == "boolean_version":
        snapshot["schema_version"] = True
    elif bad == "missing_transfers":
        del snapshot["transfers"]
    elif bad == "invalid_work_item":
        snapshot["work_item"] = []
    elif bad == "empty_objective":
        snapshot["work_item"]["objective"] = " "
    else:
        snapshot["work_item"]["input"] = []
    run.pin_work_item_snapshot(snapshot)
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []


def test_cross_task_subtask_reference_does_not_leak_to_either_tenant(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    other_task, other_subtask, _ = _run_fixture(sessions)
    run.subtask_id = other_subtask.id
    run.pin_work_item_snapshot(_snapshot())
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []
    assert _jobs(sessions, other_task) == []


def test_direct_task_run_never_creates_collaboration_notices(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions, mode=TaskExecutionMode.DIRECT)
    run.pin_work_item_snapshot(_snapshot())
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    assert _jobs(sessions, task) == []


def test_terminal_result_and_notice_rollback_together(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    run.start()
    run.pin_work_item_snapshot(_snapshot())
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    run.succeed({"summary": "Do not announce a rolled-back result"})
    with _enabled(sessions)() as uow:
        uow.runs.save(run)
        uow.rollback()
    assert {job.event_kind for job in _jobs(sessions, task)} == {"COLLAB_STARTED"}
    with _enabled(sessions)() as uow:
        persisted = uow.runs.get(run.id)
        assert persisted.status is RunStatus.RUNNING
        assert persisted.output is None
        uow.runs.save(run)
        uow.commit()
    assert {job.event_kind for job in _jobs(sessions, task)} == {"COLLAB_STARTED", "COLLAB_RESULT"}


def test_spoofed_domain_task_identity_cannot_redirect_collaboration_notice(collaboration_sessions):
    sessions = collaboration_sessions
    task, _, run = _run_fixture(sessions)
    other, _, _ = _run_fixture(sessions)
    with _enabled(sessions)() as uow:
        uow.runs.add(run)
        uow.commit()
    run.pin_work_item_snapshot(_snapshot())
    run.task_id = other.id
    with _enabled(sessions)() as uow:
        uow.runs.save(run)
        uow.commit()
    assert _jobs(sessions, task) == []
    assert _jobs(sessions, other) == []
