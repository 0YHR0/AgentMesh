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

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.budget_services import BudgetQueryService
from agentmesh.application.handoff_services import HandoffApplicationService
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.observability_services import UsageQueryService
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
from agentmesh.integrations.mcp.client import StdioMcpReadOnlyToolGateway
from agentmesh.integrations.mcp.workspace_server import SERVER_NAME, TOOL_NAME
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
    identity_service = IdentityService(
        enabled=feature_gates.is_enabled(Feature.IDENTITY_RBAC),
        tenant_id=runtime_settings.tenant_id,
        principals_json=runtime_settings.identity_principals_json,
    )
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
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
    if runtime_settings.langfuse_enabled and not feature_gates.is_enabled(
        Feature.OBSERVABILITY
    ):
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
        if feature_gates.is_enabled(Feature.MCP_READ_TOOLS):
            workspace_root = Path(runtime_settings.mcp_workspace_root).resolve(strict=True)
            gateway = StdioMcpReadOnlyToolGateway(
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
            invocation_service = ToolInvocationService(
                uow_factory=uow_factory,
                tenant_id=runtime_settings.tenant_id,
            )
        agent_executor = ReadOnlyMcpAgentExecutor(
            fallback=DeterministicAgentExecutor(),
            feature_gates=feature_gates,
            binding=binding,
            gateway=gateway,
            invocation_service=invocation_service,
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
            outbox_retention=timedelta(
                seconds=runtime_settings.outbox_retention_seconds
            ),
            inbox_retention=timedelta(
                seconds=runtime_settings.inbox_retention_seconds
            ),
            batch_size=runtime_settings.retention_batch_size,
            streams=(
                StreamRetentionPolicy(
                    stream_name=runtime_settings.execution_stream,
                    retention=timedelta(
                        seconds=runtime_settings.redis_stream_retention_seconds
                    ),
                    max_entries=runtime_settings.redis_stream_max_entries,
                    required_group=runtime_settings.execution_group,
                    protects_inbox=True,
                ),
                StreamRetentionPolicy(
                    stream_name=runtime_settings.domain_event_stream,
                    retention=timedelta(
                        seconds=runtime_settings.redis_stream_retention_seconds
                    ),
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
