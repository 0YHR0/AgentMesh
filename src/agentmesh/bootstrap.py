from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from langgraph.checkpoint.postgres import PostgresSaver
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.ports import ReadinessProbe
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.config import Settings, get_settings
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.readiness import PostgresReadinessProbe
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.messaging.outbox import (
    OutboxRelay,
    RedisStreamPublisher,
    SqlAlchemyOutboxStore,
)
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import (
    LangGraphWorkflowRunner,
    create_langfuse_callbacks,
)
from agentmesh.workers.execution import RedisRunWorker


@dataclass
class ApplicationContainer:
    task_service: TaskApplicationService
    registry_service: AgentRegistryService
    readiness_probe: ReadinessProbe
    feature_gates: FeatureGateSet
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
    registry_service = AgentRegistryService(
        uow_factory=uow_factory,
        tenant_id=runtime_settings.tenant_id,
    )
    task_service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id=runtime_settings.agent_id,
        tenant_id=runtime_settings.tenant_id,
    )
    return ApplicationContainer(
        task_service=task_service,
        registry_service=registry_service,
        readiness_probe=PostgresReadinessProbe(engine),
        feature_gates=feature_gates,
        close_callback=engine.dispose,
    )


def seed_builtin_registry(settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
    try:
        AgentRegistryService(
            uow_factory=uow_factory,
            tenant_id=runtime_settings.tenant_id,
        ).ensure_builtin_agent(runtime_settings.agent_id)
    finally:
        engine.dispose()


def build_worker_container(
    settings: Settings | None = None,
    *,
    worker_id: str,
) -> WorkerContainer:
    runtime_settings = settings or get_settings()
    engine, _session_factory, uow_factory = _database_components(runtime_settings)
    redis_client = Redis.from_url(runtime_settings.redis_url, decode_responses=True)
    checkpointer_context = PostgresSaver.from_conn_string(runtime_settings.checkpoint_database_url)
    checkpointer = checkpointer_context.__enter__()

    try:
        checkpointer.setup()
        workflow_runner = LangGraphWorkflowRunner(
            agent_executor=DeterministicAgentExecutor(),
            checkpointer=checkpointer,
            callbacks=create_langfuse_callbacks(runtime_settings.langfuse_enabled),
        )
        execution_service = RunExecutionService(
            uow_factory=uow_factory,
            workflow_runner=workflow_runner,
            worker_id=worker_id,
            consumer_name=runtime_settings.execution_consumer_name,
            lease_duration=timedelta(seconds=runtime_settings.run_lease_seconds),
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
        checkpointer_context.__exit__(None, None, None)
        redis_client.close()
        engine.dispose()
        raise

    def close() -> None:
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

    def close() -> None:
        redis_client.close()
        engine.dispose()

    return RelayContainer(relay=relay, close_callback=close)


# Compatibility alias for early integrations. The API container intentionally owns no workflow.
build_runtime_container = build_api_container
