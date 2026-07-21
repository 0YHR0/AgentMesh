from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.budget_services import BudgetQueryService
from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.application.handoff_services import HandoffApplicationService
from agentmesh.application.identity_services import IdentityAdministrationService, IdentityService
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.policy_services import DEFAULT_POLICY_RULES, PolicyApprovalService
from agentmesh.application.ports import ReadinessProbe
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.config import Settings, get_settings
from agentmesh.domain.errors import InvalidFeatureConfiguration
from agentmesh.domain.tools import WORKSPACE_READ_TOOL_KEY, ToolBinding, ToolSideEffect
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.infrastructure.postgres.readiness import PostgresReadinessProbe
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.integrations.a2a.client import PinnedHttpsA2AClient
from agentmesh.integrations.credentials import EnvironmentSecretValueProvider
from agentmesh.integrations.mcp.client import (
    RoutedMcpReadOnlyToolGateway,
    StdioMcpReadOnlyToolGateway,
    StreamableHttpMcpDiscoveryGateway,
    StreamableHttpMcpReadOnlyToolGateway,
)
from agentmesh.integrations.mcp.workspace_server import INPUT_SCHEMA, SERVER_NAME, TOOL_NAME
from agentmesh.integrations.oidc import OidcJwtVerifier
from agentmesh.maintenance.retention import (
    MessagingRetentionPolicy,
    MessagingRetentionService,
    RedisStreamRetentionStore,
    RetentionScheduler,
    SqlAlchemyMessageRetentionStore,
    StreamRetentionPolicy,
)
from agentmesh.messaging.outbox import (
    OutboxRelay,
    RedisStreamPublisher,
    SqlAlchemyOutboxStore,
)
from agentmesh.observability import create_attempt_telemetry
from agentmesh.orchestration.agent import (
    DeterministicAcceptanceReviewer,
    DeterministicAgentExecutor,
)
from agentmesh.orchestration.mcp_agent import ReadOnlyMcpAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from agentmesh.workers.a2a_reconciliation import A2AReconciliationWorker
from agentmesh.workers.execution import RedisRunWorker


@dataclass
class ApplicationContainer:
    task_service: TaskApplicationService
    handoff_service: HandoffApplicationService
    registry_service: AgentRegistryService
    artifact_service: ArtifactService
    tool_invocation_service: ToolInvocationService
    usage_service: UsageQueryService
    budget_service: BudgetQueryService
    resolution_service: TaskResolutionService
    readiness_probe: ReadinessProbe
    feature_gates: FeatureGateSet
    identity_service: IdentityService
    identity_administration_service: IdentityAdministrationService
    policy_service: PolicyApprovalService
    mcp_registry_service: McpRegistryService
    a2a_registry_service: A2ARegistryService
    a2a_delegation_service: A2ADelegationService
    credential_broker_service: CredentialBrokerService
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


@dataclass
class WorkerContainer:
    worker: RedisRunWorker
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


@dataclass
class A2AReconcilerContainer:
    worker: A2AReconciliationWorker
    scan_interval_seconds: int
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


@dataclass
class RelayContainer:
    relay: OutboxRelay
    retention: RetentionScheduler
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


def _database_components(settings: Settings):
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    return engine, session_factory, SqlAlchemyUnitOfWorkFactory(session_factory)


def build_api_container(settings: Settings | None = None) -> ApplicationContainer:
    runtime_settings = settings or get_settings()
    feature_gates = FeatureGateSet.from_config(
        runtime_settings.feature_profile,
        runtime_settings.feature_gates,
    )
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
    persistent_identity = feature_gates.is_enabled(Feature.PERSISTENT_IDENTITY)
    oidc_verifier = None
    issuer = (runtime_settings.identity_oidc_issuer or "").strip()
    audience = (runtime_settings.identity_oidc_audience or "").strip()
    if bool(issuer) != bool(audience):
        raise InvalidFeatureConfiguration("OIDC issuer and audience must be configured together")
    if issuer and not persistent_identity:
        raise InvalidFeatureConfiguration(
            "OIDC configuration requires the 'persistent_identity' feature"
        )
    if persistent_identity and issuer:
        oidc_verifier = OidcJwtVerifier(
            issuer=issuer,
            audience=audience,
            cache_seconds=runtime_settings.identity_oidc_jwks_cache_seconds,
        )
    identity_service = IdentityService(
        enabled=feature_gates.is_enabled(Feature.IDENTITY_RBAC),
        tenant_id=runtime_settings.tenant_id,
        principals_json=runtime_settings.identity_principals_json,
        persistent=persistent_identity,
        uow_factory=uow_factory if persistent_identity else None,
        oidc_verifier=oidc_verifier,
    )
    identity_administration_service = IdentityAdministrationService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    if persistent_identity:
        identity_administration_service.bootstrap(identity_service.configured_principals)
    registry_service = AgentRegistryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    task_service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id=runtime_settings.agent_id,
        tenant_id=runtime_settings.tenant_id,
        reviewer_agent_id=runtime_settings.reviewer_agent_id,
        max_review_revisions=runtime_settings.review_max_revisions,
        supervisor_agent_id=runtime_settings.supervisor_agent_id,
        max_coordinated_concurrency=runtime_settings.coordinated_max_concurrency,
        feature_gates=feature_gates,
    )
    handoff_service = HandoffApplicationService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        supervisor_agent_id=runtime_settings.supervisor_agent_id,
        feature_gates=feature_gates,
    )
    artifact_service = ArtifactService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        owner_id=runtime_settings.artifact_owner_id,
        max_inline_bytes=runtime_settings.artifact_max_inline_bytes,
    )
    tool_invocation_service = ToolInvocationService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    usage_service = UsageQueryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    budget_service = BudgetQueryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    resolution_service = TaskResolutionService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        executor_agent_id=runtime_settings.agent_id,
        reviewer_agent_id=runtime_settings.reviewer_agent_id,
        supervisor_agent_id=runtime_settings.supervisor_agent_id,
        feature_gates=feature_gates,
    )
    policy_service = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        enabled=feature_gates.is_enabled(Feature.POLICY_APPROVAL),
        rules_json=runtime_settings.policy_rules_json or DEFAULT_POLICY_RULES,
        ttl=timedelta(seconds=runtime_settings.policy_action_ttl_seconds),
    )
    mcp_registry_service = McpRegistryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        policy_service=policy_service,
        discovery_gateway=StreamableHttpMcpDiscoveryGateway(
            timeout_seconds=runtime_settings.mcp_http_timeout_seconds,
            max_response_bytes=runtime_settings.mcp_max_result_bytes,
            max_tools=runtime_settings.mcp_discovery_max_tools,
        ),
        discovery_ttl_seconds=runtime_settings.mcp_discovery_ttl_seconds,
    )
    a2a_client = PinnedHttpsA2AClient(
        timeout_seconds=runtime_settings.a2a_timeout_seconds,
        max_request_bytes=runtime_settings.a2a_max_request_bytes,
        max_response_bytes=runtime_settings.a2a_max_response_bytes,
    )
    a2a_registry_service = A2ARegistryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        discovery_client=a2a_client,
        discovery_default_ttl_seconds=runtime_settings.a2a_discovery_default_ttl_seconds,
    )
    if (
        feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
        and runtime_settings.credential_workload_principal_id is None
    ):
        raise InvalidFeatureConfiguration(
            "The 'credential_broker' feature requires credential_workload_principal_id"
        )
    credential_broker_service = CredentialBrokerService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        policy_service=policy_service,
        provider=EnvironmentSecretValueProvider(),
        lease_ttl_seconds=runtime_settings.credential_lease_ttl_seconds,
        environment=runtime_settings.environment,
    )
    a2a_delegation_service = A2ADelegationService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        policy_service=policy_service,
        client=a2a_client,
        credential_broker=(
            credential_broker_service
            if feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
            else None
        ),
        workload_principal_id=runtime_settings.credential_workload_principal_id,
        max_inline_result_bytes=runtime_settings.a2a_max_inline_result_bytes,
        poll_interval=timedelta(seconds=runtime_settings.a2a_poll_interval_seconds),
        poll_lease_duration=timedelta(seconds=runtime_settings.a2a_poll_lease_seconds),
        poll_failure_base_delay=timedelta(seconds=runtime_settings.a2a_poll_failure_base_seconds),
        poll_failure_max_delay=timedelta(seconds=runtime_settings.a2a_poll_failure_max_seconds),
        poll_max_failures=runtime_settings.a2a_poll_max_failures,
    )
    return ApplicationContainer(
        task_service=task_service,
        handoff_service=handoff_service,
        registry_service=registry_service,
        artifact_service=artifact_service,
        tool_invocation_service=tool_invocation_service,
        usage_service=usage_service,
        budget_service=budget_service,
        resolution_service=resolution_service,
        readiness_probe=PostgresReadinessProbe(engine),
        feature_gates=feature_gates,
        identity_service=identity_service,
        identity_administration_service=identity_administration_service,
        policy_service=policy_service,
        mcp_registry_service=mcp_registry_service,
        a2a_registry_service=a2a_registry_service,
        a2a_delegation_service=a2a_delegation_service,
        credential_broker_service=credential_broker_service,
        close_callback=engine.dispose,
    )


def seed_builtin_registry(settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
    try:
        registry = AgentRegistryService(
            uow_factory=uow_factory,
            tenant_id=runtime_settings.tenant_id,
        )
        registry.ensure_builtin_agent(runtime_settings.agent_id)
        registry.ensure_builtin_agent(runtime_settings.reviewer_agent_id, reviewer=True)
        registry.ensure_builtin_agent(runtime_settings.supervisor_agent_id, supervisor=True)
        policy = PolicyApprovalService(
            uow_factory=uow_factory,
            tenant_id=runtime_settings.tenant_id,
            enabled=False,
        )
        McpRegistryService(
            uow_factory=uow_factory,
            tenant_id=runtime_settings.tenant_id,
            policy_service=policy,
        ).ensure_builtin_workspace(
            server_name=SERVER_NAME,
            tool_name=TOOL_NAME,
            logical_key=WORKSPACE_READ_TOOL_KEY,
            input_schema=INPUT_SCHEMA,
        )
    finally:
        engine.dispose()


def build_worker_container(
    settings: Settings | None = None,
    *,
    worker_id: str,
) -> WorkerContainer:
    runtime_settings = settings or get_settings()
    feature_gates = FeatureGateSet.from_config(
        runtime_settings.feature_profile,
        runtime_settings.feature_gates,
    )
    if runtime_settings.langfuse_enabled and not feature_gates.is_enabled(Feature.OBSERVABILITY):
        raise InvalidFeatureConfiguration(
            "Langfuse export requires the 'observability' feature to be enabled"
        )
    public_key = (runtime_settings.langfuse_public_key or "").strip() or None
    secret_key = (runtime_settings.langfuse_secret_key or "").strip() or None
    if runtime_settings.langfuse_enabled and (public_key is None or secret_key is None):
        raise InvalidFeatureConfiguration(
            "Langfuse export requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"
        )
    telemetry = create_attempt_telemetry(
        enabled=runtime_settings.langfuse_enabled,
        public_key=public_key,
        secret_key=secret_key,
        base_url=(runtime_settings.langfuse_base_url or "").strip() or None,
        environment=runtime_settings.environment,
        timeout_seconds=runtime_settings.langfuse_timeout_seconds,
    )
    engine = None
    redis_client = None
    checkpointer_context = None
    checkpointer_open = False
    try:
        engine, _session_factory, uow_factory = _database_components(runtime_settings)
        redis_client = Redis.from_url(runtime_settings.redis_url, decode_responses=True)
        checkpointer_context = PostgresSaver.from_conn_string(
            runtime_settings.checkpoint_database_url
        )
        checkpointer = checkpointer_context.__enter__()
        checkpointer_open = True
        checkpointer.setup()
        binding = ToolBinding(
            logical_key=WORKSPACE_READ_TOOL_KEY,
            server_name=SERVER_NAME,
            tool_name=TOOL_NAME,
            side_effect=ToolSideEffect.READ_ONLY,
        )
        gateway = None
        invocation_service = None
        catalog = None
        if feature_gates.is_enabled(Feature.MCP_READ_TOOLS):
            workspace_root = Path(runtime_settings.mcp_workspace_root).resolve(strict=True)
            stdio_gateway = StdioMcpReadOnlyToolGateway(
                command=sys.executable,
                arguments=["-m", "agentmesh.integrations.mcp.workspace_server"],
                environment={
                    "AGENTMESH_MCP_WORKSPACE_ROOT": str(workspace_root),
                    "AGENTMESH_MCP_WORKSPACE_MAX_BYTES": str(
                        runtime_settings.mcp_workspace_max_bytes
                    ),
                },
                working_directory=workspace_root,
                timeout_seconds=runtime_settings.mcp_workspace_timeout_seconds,
                max_result_bytes=runtime_settings.mcp_max_result_bytes,
            )
            if (
                feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
                and runtime_settings.credential_workload_principal_id is None
            ):
                raise InvalidFeatureConfiguration(
                    "The 'credential_broker' feature requires credential_workload_principal_id"
                )
            worker_policy = PolicyApprovalService(
                uow_factory=uow_factory,
                tenant_id=runtime_settings.tenant_id,
                enabled=False,
            )
            worker_credential_broker = CredentialBrokerService(
                uow_factory=uow_factory,
                tenant_id=runtime_settings.tenant_id,
                policy_service=worker_policy,
                provider=EnvironmentSecretValueProvider(),
                lease_ttl_seconds=runtime_settings.credential_lease_ttl_seconds,
                environment=runtime_settings.environment,
            )
            http_gateway = StreamableHttpMcpReadOnlyToolGateway(
                timeout_seconds=runtime_settings.mcp_http_timeout_seconds,
                max_result_bytes=runtime_settings.mcp_max_result_bytes,
                credential_broker=(
                    worker_credential_broker
                    if feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
                    else None
                ),
                workload_principal_id=runtime_settings.credential_workload_principal_id,
            )
            gateway = RoutedMcpReadOnlyToolGateway(
                stdio=stdio_gateway,
                streamable_http=http_gateway,
            )
            invocation_service = ToolInvocationService(
                uow_factory=uow_factory,
                tenant_id=runtime_settings.tenant_id,
            )
            if feature_gates.is_enabled(Feature.GOVERNED_MCP):
                catalog = McpRegistryService(
                    uow_factory=uow_factory,
                    tenant_id=runtime_settings.tenant_id,
                    policy_service=PolicyApprovalService(
                        uow_factory=uow_factory,
                        tenant_id=runtime_settings.tenant_id,
                        enabled=False,
                    ),
                )
        agent_executor = ReadOnlyMcpAgentExecutor(
            fallback=DeterministicAgentExecutor(),
            feature_gates=feature_gates,
            binding=binding,
            gateway=gateway,
            invocation_service=invocation_service,
            catalog=catalog,
        )
        workflow_runner = LangGraphWorkflowRunner(
            agent_executor=agent_executor,
            reviewer_executor=DeterministicAcceptanceReviewer(),
            checkpointer=checkpointer,
            telemetry=telemetry,
        )
        execution_service = RunExecutionService(
            uow_factory=uow_factory,
            workflow_runner=workflow_runner,
            worker_id=worker_id,
            consumer_name=runtime_settings.execution_consumer_name,
            lease_duration=timedelta(seconds=runtime_settings.run_lease_seconds),
            executor_agent_id=runtime_settings.agent_id,
            reviewer_agent_id=runtime_settings.reviewer_agent_id,
            supervisor_agent_id=runtime_settings.supervisor_agent_id,
            lease_renewal_interval=(
                timedelta(seconds=runtime_settings.run_lease_renewal_seconds)
                if runtime_settings.run_lease_renewal_seconds is not None
                else None
            ),
        )
        worker = RedisRunWorker(
            redis_client=redis_client,
            execution_service=execution_service,
            stream_name=runtime_settings.execution_stream,
            group_name=runtime_settings.execution_group,
            consumer_id=worker_id,
            dead_letter_stream=runtime_settings.dead_letter_stream,
            block_ms=runtime_settings.worker_block_ms,
            pending_idle_ms=runtime_settings.worker_pending_idle_ms,
        )
    except Exception:
        telemetry.close()
        if checkpointer_open and checkpointer_context is not None:
            checkpointer_context.__exit__(None, None, None)
        if redis_client is not None:
            redis_client.close()
        if engine is not None:
            engine.dispose()
        raise

    def close() -> None:
        telemetry.close()
        assert checkpointer_context is not None
        assert redis_client is not None
        assert engine is not None
        checkpointer_context.__exit__(None, None, None)
        redis_client.close()
        engine.dispose()

    return WorkerContainer(worker=worker, close_callback=close)


def build_a2a_reconciler_container(
    settings: Settings | None = None,
    *,
    worker_id: str,
) -> A2AReconcilerContainer:
    runtime_settings = settings or get_settings()
    feature_gates = FeatureGateSet.from_config(
        runtime_settings.feature_profile,
        runtime_settings.feature_gates,
    )
    feature_gates.require(Feature.A2A_RECONCILIATION)
    if (
        feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
        and runtime_settings.credential_workload_principal_id is None
    ):
        raise InvalidFeatureConfiguration(
            "The 'credential_broker' feature requires credential_workload_principal_id"
        )
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
    policy = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        enabled=False,
    )
    credential_broker = CredentialBrokerService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        policy_service=policy,
        provider=EnvironmentSecretValueProvider(),
        lease_ttl_seconds=runtime_settings.credential_lease_ttl_seconds,
        environment=runtime_settings.environment,
    )
    service = A2ADelegationService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
        policy_service=policy,
        client=PinnedHttpsA2AClient(
            timeout_seconds=runtime_settings.a2a_timeout_seconds,
            max_request_bytes=runtime_settings.a2a_max_request_bytes,
            max_response_bytes=runtime_settings.a2a_max_response_bytes,
        ),
        credential_broker=(
            credential_broker if feature_gates.is_enabled(Feature.CREDENTIAL_BROKER) else None
        ),
        workload_principal_id=runtime_settings.credential_workload_principal_id,
        max_inline_result_bytes=runtime_settings.a2a_max_inline_result_bytes,
        poll_interval=timedelta(seconds=runtime_settings.a2a_poll_interval_seconds),
        poll_lease_duration=timedelta(seconds=runtime_settings.a2a_poll_lease_seconds),
        poll_failure_base_delay=timedelta(seconds=runtime_settings.a2a_poll_failure_base_seconds),
        poll_failure_max_delay=timedelta(seconds=runtime_settings.a2a_poll_failure_max_seconds),
        poll_max_failures=runtime_settings.a2a_poll_max_failures,
    )
    worker = A2AReconciliationWorker(
        service=service,
        worker_id=worker_id,
        batch_size=runtime_settings.a2a_reconciliation_batch_size,
    )
    return A2AReconcilerContainer(
        worker=worker,
        scan_interval_seconds=runtime_settings.a2a_reconciliation_scan_seconds,
        close_callback=engine.dispose,
    )


def build_relay_container(
    settings: Settings | None = None,
    *,
    relay_id: str,
) -> RelayContainer:
    runtime_settings = settings or get_settings()
    engine, session_factory, _uow_factory = _database_components(runtime_settings)
    redis_client = Redis.from_url(runtime_settings.redis_url, decode_responses=True)
    relay = OutboxRelay(
        relay_id=relay_id,
        store=SqlAlchemyOutboxStore(session_factory),
        publisher=RedisStreamPublisher(
            redis_client,
            runtime_settings.execution_stream,
            runtime_settings.domain_event_stream,
        ),
        batch_size=runtime_settings.relay_batch_size,
        claim_duration=timedelta(seconds=runtime_settings.relay_claim_seconds),
        retry_delay=timedelta(seconds=runtime_settings.relay_retry_seconds),
    )
    retention_service = MessagingRetentionService(
        database=SqlAlchemyMessageRetentionStore(session_factory),
        streams=RedisStreamRetentionStore(redis_client),
        policy=MessagingRetentionPolicy(
            outbox_retention=timedelta(seconds=runtime_settings.outbox_retention_seconds),
            inbox_retention=timedelta(seconds=runtime_settings.inbox_retention_seconds),
            batch_size=runtime_settings.retention_batch_size,
            streams=(
                StreamRetentionPolicy(
                    stream_name=runtime_settings.execution_stream,
                    retention=timedelta(seconds=runtime_settings.redis_stream_retention_seconds),
                    max_entries=runtime_settings.redis_stream_max_entries,
                    required_group=runtime_settings.execution_group,
                    protects_inbox=True,
                ),
                StreamRetentionPolicy(
                    stream_name=runtime_settings.domain_event_stream,
                    retention=timedelta(seconds=runtime_settings.redis_stream_retention_seconds),
                    max_entries=runtime_settings.redis_stream_max_entries,
                ),
                StreamRetentionPolicy(
                    stream_name=runtime_settings.dead_letter_stream,
                    retention=timedelta(
                        seconds=runtime_settings.dead_letter_stream_retention_seconds
                    ),
                    max_entries=runtime_settings.dead_letter_stream_max_entries,
                ),
            ),
        ),
    )
    retention = RetentionScheduler(
        service=retention_service,
        interval=timedelta(seconds=runtime_settings.retention_interval_seconds),
    )

    def close() -> None:
        redis_client.close()
        engine.dispose()

    return RelayContainer(relay=relay, retention=retention, close_callback=close)


# Compatibility alias for early integrations. The API container intentionally owns no workflow.
build_runtime_container = build_api_container
