import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.services import TaskApplicationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import AlwaysReady, InMemoryUnitOfWorkFactory


@pytest.fixture
def uow_factory() -> InMemoryUnitOfWorkFactory:
    return InMemoryUnitOfWorkFactory()


@pytest.fixture
def task_service(uow_factory: InMemoryUnitOfWorkFactory) -> TaskApplicationService:
    workflow = LangGraphWorkflowRunner(
        agent_executor=DeterministicAgentExecutor(),
        checkpointer=InMemorySaver(),
    )
    return TaskApplicationService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        agent_id="test-agent",
    )


@pytest.fixture
def application_container(task_service: TaskApplicationService) -> ApplicationContainer:
    return ApplicationContainer(task_service=task_service, readiness_probe=AlwaysReady())
