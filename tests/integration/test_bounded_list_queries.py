from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.config import get_settings
from agentmesh.domain.artifacts import Artifact, ArtifactClassification
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_task_list_query_count_is_bounded() -> None:
    suffix = uuid4().hex
    settings = get_settings().model_copy(
        update={
            "tenant_id": f"bounded-task-{suffix}",
            "agent_id": "test-agent",
        }
    )
    engine = create_engine(settings.database_url)
    uow_factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    registry = AgentRegistryService(uow_factory=uow_factory, tenant_id=settings.tenant_id)
    task_service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id=settings.agent_id,
        tenant_id=settings.tenant_id,
    )
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=DeterministicAgentExecutor(),
            checkpointer=InMemorySaver(),
        ),
        worker_id=f"bounded-worker-{suffix}",
        consumer_name=f"bounded-consumer-{suffix}",
        lease_duration=timedelta(seconds=settings.run_lease_seconds),
    )

    try:
        registry.ensure_builtin_agent(settings.agent_id)
        created_only = task_service.create_task("Created only")
        queued_task = task_service.create_task("Queued only")
        task_service.request_run(queued_task.task.id)
        completed_task = task_service.create_task("Completed")
        completed = task_service.request_run(completed_task.task.id)
        assert execution_service.process(
            MessageEnvelope.run_requested(
                tenant_id=settings.tenant_id,
                task_id=completed_task.task.id,
                run_id=completed.runs[0].id,
            )
        )

        other_tenant = f"other-{suffix}"
        AgentRegistryService(uow_factory=uow_factory, tenant_id=other_tenant).ensure_builtin_agent(
            settings.agent_id
        )
        other_service = TaskApplicationService(
            uow_factory=uow_factory,
            agent_id=settings.agent_id,
            tenant_id=other_tenant,
        )
        other_task = other_service.create_task("Other tenant")

        with count_selects(engine) as select_count:
            values = task_service.list_tasks(limit=10, offset=0)

        by_id = {value.task.id: value for value in values}
        assert set(by_id) == {created_only.task.id, queued_task.task.id, completed_task.task.id}
        assert other_task.task.id not in by_id
        assert by_id[created_only.task.id].runs == []
        assert len(by_id[queued_task.task.id].runs) == 1
        assert by_id[queued_task.task.id].attempts == []
        assert len(by_id[completed_task.task.id].runs) == 1
        assert len(by_id[completed_task.task.id].attempts) == 1
        assert select_count() == 6
    finally:
        cleanup_outbox(engine, settings.tenant_id, f"other-{suffix}")
        engine.dispose()


def test_artifact_list_query_count_is_bounded() -> None:
    suffix = uuid4().hex
    settings = get_settings().model_copy(update={"tenant_id": f"bounded-artifact-{suffix}"})
    engine = create_engine(settings.database_url)
    uow_factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    artifact_service = ArtifactService(
        uow_factory=uow_factory,
        tenant_id=settings.tenant_id,
        owner_id="test-user",
        max_inline_bytes=settings.artifact_max_inline_bytes,
    )

    try:
        versioned = artifact_service.create_artifact(
            display_name="versioned.json",
            kind="task.result",
            classification=ArtifactClassification.INTERNAL,
            media_type="application/json",
            content=b'{"version":1}',
        )
        artifact_service.add_version(
            versioned.artifact.id,
            media_type="application/json",
            content=b'{"version":2}',
        )
        no_version = Artifact.create(
            tenant_id=settings.tenant_id,
            owner_id="test-user",
            display_name="reserved.txt",
            kind="document.text",
            classification=ArtifactClassification.INTERNAL,
        )
        with uow_factory() as uow:
            uow.artifacts.add(no_version)
            uow.commit()

        other_service = ArtifactService(
            uow_factory=uow_factory,
            tenant_id=f"other-{suffix}",
            owner_id="test-user",
            max_inline_bytes=settings.artifact_max_inline_bytes,
        )
        other = other_service.create_artifact(
            display_name="other.txt",
            kind="document.text",
            classification=ArtifactClassification.INTERNAL,
            media_type="text/plain",
            content=b"other",
        )

        with count_selects(engine) as select_count:
            values = artifact_service.list_artifacts(limit=10, offset=0)

        by_id = {value.artifact.id: value for value in values}
        assert set(by_id) == {versioned.artifact.id, no_version.id}
        assert other.artifact.id not in by_id
        assert [version.version_number for version in by_id[versioned.artifact.id].versions] == [
            1,
            2,
        ]
        assert by_id[no_version.id].versions == []
        assert select_count() == 2
    finally:
        cleanup_outbox(engine, settings.tenant_id, f"other-{suffix}")
        engine.dispose()


@contextmanager
def count_selects(engine: Engine) -> Iterator[Callable[[], int]]:
    count = 0

    def before_cursor_execute(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ) -> None:
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield lambda: count
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def cleanup_outbox(engine: Engine, *tenant_ids: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = ANY(:tenant_ids)"),
            {"tenant_ids": list(tenant_ids)},
        )
