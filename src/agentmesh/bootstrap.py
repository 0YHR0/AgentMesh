from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.ports import ReadinessProbe
from agentmesh.application.services import TaskApplicationService
from agentmesh.config import Settings, get_settings
from agentmesh.infrastructure.postgres.readiness import PostgresReadinessProbe
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import (
    LangGraphWorkflowRunner,
    create_langfuse_callbacks,
)


@dataclass
class ApplicationContainer:
    task_service: TaskApplicationService
    readiness_probe: ReadinessProbe
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


def build_runtime_container(settings: Settings | None = None) -> ApplicationContainer:
    runtime_settings = settings or get_settings()
    engine = create_engine(runtime_settings.database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)

    checkpointer_context = PostgresSaver.from_conn_string(
        runtime_settings.checkpoint_database_url
    )
    checkpointer = checkpointer_context.__enter__()

    try:
        checkpointer.setup()
        workflow_runner = LangGraphWorkflowRunner(
            agent_executor=DeterministicAgentExecutor(),
            checkpointer=checkpointer,
            callbacks=create_langfuse_callbacks(runtime_settings.langfuse_enabled),
        )
        task_service = TaskApplicationService(
            uow_factory=uow_factory,
            workflow_runner=workflow_runner,
            agent_id=runtime_settings.agent_id,
        )
    except Exception:
        checkpointer_context.__exit__(None, None, None)
        engine.dispose()
        raise

    def close() -> None:
        checkpointer_context.__exit__(None, None, None)
        engine.dispose()

    return ApplicationContainer(
        task_service=task_service,
        readiness_probe=PostgresReadinessProbe(engine),
        close_callback=close,
    )
