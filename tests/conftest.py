from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.activity_services import TaskActivityService
from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.budget_services import BudgetQueryService
from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.application.company_goal_services import CompanyGoalService
from agentmesh.application.company_operation_services import CompanyOperationService
from agentmesh.application.company_pack_services import CompanyPackService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.application.financial_governance_services import (
    FinancialGovernanceService,
)
from agentmesh.application.handoff_services import HandoffApplicationService
from agentmesh.application.identity_services import IdentityAdministrationService, IdentityService
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.office_services import OfficeLayoutService
from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
)
from agentmesh.application.planning_services import PlanningApplicationService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.features import FeatureGateSet
from agentmesh.integrations.credentials import EnvironmentSecretValueProvider
from agentmesh.orchestration.agent import (
    DeterministicAcceptanceReviewer,
    DeterministicAgentExecutor,
)
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import (
    AlwaysReady,
    InMemoryOfficePlacementStore,
    InMemoryUnitOfWorkFactory,
    ScriptedA2AClient,
)


@pytest.fixture
def uow_factory() -> InMemoryUnitOfWorkFactory:
    return InMemoryUnitOfWorkFactory()


@pytest.fixture
def registry_service(uow_factory: InMemoryUnitOfWorkFactory) -> AgentRegistryService:
    service = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    service.ensure_builtin_agent("test-agent")
    service.ensure_builtin_agent("test-reviewer", reviewer=True)
    service.ensure_builtin_agent("test-supervisor", supervisor=True)
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
        reviewer_agent_id="test-reviewer",
        supervisor_agent_id="test-supervisor",
        feature_gates=FeatureGateSet.from_config("full"),
    )


@pytest.fixture
def execution_service(uow_factory: InMemoryUnitOfWorkFactory) -> RunExecutionService:
    workflow = LangGraphWorkflowRunner(
        agent_executor=DeterministicAgentExecutor(),
        reviewer_executor=DeterministicAcceptanceReviewer(),
        checkpointer=InMemorySaver(),
    )
    return RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="test-worker",
        consumer_name="test-run-executor-v1",
        lease_duration=timedelta(minutes=5),
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        supervisor_agent_id="test-supervisor",
    )


@pytest.fixture
def handoff_service(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> HandoffApplicationService:
    return HandoffApplicationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        supervisor_agent_id="test-supervisor",
        feature_gates=FeatureGateSet.from_config("full"),
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
def budget_service(uow_factory: InMemoryUnitOfWorkFactory) -> BudgetQueryService:
    return BudgetQueryService(uow_factory=uow_factory, tenant_id="test-tenant")


@pytest.fixture
def resolution_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> TaskResolutionService:
    return TaskResolutionService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        supervisor_agent_id="test-supervisor",
        feature_gates=FeatureGateSet.from_config("full"),
    )


@pytest.fixture
def planning_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> PlanningApplicationService:
    return PlanningApplicationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        max_concurrency=4,
        feature_gates=FeatureGateSet.from_config("full"),
    )


@pytest.fixture
def company_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> CompanyModelService:
    return CompanyModelService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )


@pytest.fixture
def company_goal_service(
    uow_factory: InMemoryUnitOfWorkFactory,
    task_service: TaskApplicationService,
) -> CompanyGoalService:
    return CompanyGoalService(
        uow_factory=uow_factory,
        task_service=task_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,company_goals=true"
        ),
    )


@pytest.fixture
def company_operation_service(
    uow_factory: InMemoryUnitOfWorkFactory,
    task_service: TaskApplicationService,
) -> CompanyOperationService:
    return CompanyOperationService(
        uow_factory=uow_factory,
        task_service=task_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "company_model=true,company_goals=true,company_operations=true",
        ),
    )


@pytest.fixture
def business_object_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> BusinessObjectService:
    return BusinessObjectService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,business_objects=true"
        ),
    )


@pytest.fixture
def organizational_memory_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> OrganizationalMemoryService:
    return OrganizationalMemoryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )


@pytest.fixture
def financial_governance_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> FinancialGovernanceService:
    return FinancialGovernanceService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "company_model=true,company_finance_read=true,financial_governance=true",
        ),
    )


@pytest.fixture
def company_pack_service(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> CompanyPackService:
    return CompanyPackService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            (
                "company_model=true,company_goals=true,company_operations=true,"
                "business_objects=true,organizational_memory=true,"
                "company_finance_read=true,financial_governance=true,"
                "company_packs=true"
            ),
        ),
    )


@pytest.fixture
def application_container(
    uow_factory: InMemoryUnitOfWorkFactory,
    task_service: TaskApplicationService,
    planning_service: PlanningApplicationService,
    handoff_service: HandoffApplicationService,
    registry_service: AgentRegistryService,
    artifact_service: ArtifactService,
    tool_invocation_service: ToolInvocationService,
    usage_service: UsageQueryService,
    budget_service: BudgetQueryService,
    resolution_service: TaskResolutionService,
    company_service: CompanyModelService,
    company_goal_service: CompanyGoalService,
    company_operation_service: CompanyOperationService,
    business_object_service: BusinessObjectService,
    organizational_memory_service: OrganizationalMemoryService,
    financial_governance_service: FinancialGovernanceService,
    company_pack_service: CompanyPackService,
) -> ApplicationContainer:
    return ApplicationContainer(
        task_service=task_service,
        planning_service=planning_service,
        handoff_service=handoff_service,
        registry_service=registry_service,
        artifact_service=artifact_service,
        tool_invocation_service=tool_invocation_service,
        usage_service=usage_service,
        budget_service=budget_service,
        resolution_service=resolution_service,
        readiness_probe=AlwaysReady(),
        feature_gates=FeatureGateSet.from_config("full"),
        identity_service=IdentityService(enabled=False, tenant_id="test-tenant"),
        identity_administration_service=IdentityAdministrationService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
        ),
        policy_service=PolicyApprovalService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            enabled=False,
        ),
        mcp_registry_service=McpRegistryService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            policy_service=PolicyApprovalService(
                uow_factory=uow_factory,
                tenant_id="test-tenant",
                enabled=False,
            ),
        ),
        a2a_registry_service=A2ARegistryService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
        ),
        a2a_delegation_service=A2ADelegationService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            policy_service=PolicyApprovalService(
                uow_factory=uow_factory,
                tenant_id="test-tenant",
                enabled=False,
            ),
            client=ScriptedA2AClient(),
        ),
        credential_broker_service=CredentialBrokerService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            policy_service=PolicyApprovalService(
                uow_factory=uow_factory,
                tenant_id="test-tenant",
                enabled=False,
            ),
            provider=EnvironmentSecretValueProvider(),
            environment="test",
        ),
        quota_policy_service=QuotaPolicyService(
            uow_factory=uow_factory, tenant_id="test-tenant"
        ),
        activity_service=TaskActivityService(
            uow_factory=uow_factory, tenant_id="test-tenant"
        ),
        office_layout_service=OfficeLayoutService(
            store=InMemoryOfficePlacementStore(),
            tenant_id="test-tenant",
        ),
        company_service=company_service,
        company_goal_service=company_goal_service,
        company_operation_service=company_operation_service,
        business_object_service=business_object_service,
        organizational_memory_service=organizational_memory_service,
        financial_governance_service=financial_governance_service,
        company_pack_service=company_pack_service,
    )
