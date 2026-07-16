from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.features import FeatureGateSet
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import AlwaysReady, InMemoryUnitOfWorkFactory


@pytest.fixture
def uow_factory() -> InMemoryUnitOfWorkFactory:
    return InMemoryUnitOfWorkFactory()


@pytest.fixture
def registry_service(uow_factory: InMemoryUnitOfWorkFactory) -> AgentRegistryService:
    service = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    service.ensure_builtin_agent("test-agent")
    uow_factory.store.outbox.clear()
    return service


@pytest.fixture
def task_service(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> TaskApplicationService:
    return TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full"),
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
def artifact_service(uow_factory: InMemoryUnitOfWorkFactory) -> ArtifactService:
    return ArtifactService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        owner_id="test-user",
        max_inline_bytes=65_536,
    )


@pytest.fixture
def tool_invocation_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> ToolInvocationService:
    return ToolInvocationService(uow_factory=uow_factory, tenant_id="test-tenant")


@pytest.fixture
def usage_service(uow_factory: InMemoryUnitOfWorkFactory) -> UsageQueryService:
    return UsageQueryService(uow_factory=uow_factory, tenant_id="test-tenant")


@pytest.fixture
def application_container(
    task_service: TaskApplicationService,
    registry_service: AgentRegistryService,
    artifact_service: ArtifactService,
    tool_invocation_service: ToolInvocationService,
    usage_service: UsageQueryService,
) -> ApplicationContainer:
    return ApplicationContainer(
        task_service=task_service,
        registry_service=registry_service,
        artifact_service=artifact_service,
        tool_invocation_service=tool_invocation_service,
        usage_service=usage_service,
        readiness_probe=AlwaysReady(),
        feature_gates=FeatureGateSet.from_config("full"),
    )
