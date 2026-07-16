from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import AlwaysReady, InMemoryUnitOfWorkFactory


@pytest.fixture
def uow_factory() -> InMemoryUnitOfWorkFactory:
    return InMemoryUnitOfWorkFactory()


@pytest.fixture
def task_service(uow_factory: InMemoryUnitOfWorkFactory) -> TaskApplicationService:
    return TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
    )


@pytest.fixture
def execution_service(uow_factory: InMemoryUnitOfWorkFactory) -> RunExecutionService:
    workflow = LangGraphWorkflowRunner(
        agent_executor=DeterministicAgentExecutor(),
        checkpointer=InMemorySaver(),
    )
    return RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="test-worker",
        consumer_name="test-run-executor-v1",
        lease_duration=timedelta(minutes=5),
    )


@pytest.fixture
def application_container(task_service: TaskApplicationService) -> ApplicationContainer:
    return ApplicationContainer(task_service=task_service, readiness_probe=AlwaysReady())
