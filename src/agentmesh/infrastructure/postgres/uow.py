from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from agentmesh.domain.errors import ConcurrentTaskUpdate
from agentmesh.infrastructure.postgres.a2a_delegation_repositories import (
    SqlAlchemyRemoteTaskCorrelationRepository,
)
from agentmesh.infrastructure.postgres.a2a_registry_repositories import (
    SqlAlchemyA2ARegistryRepository,
)
from agentmesh.infrastructure.postgres.artifact_repositories import (
    SqlAlchemyArtifactRepository,
    SqlAlchemyArtifactVersionRepository,
)
from agentmesh.infrastructure.postgres.credential_repositories import (
    SqlAlchemyCredentialRepository,
)
from agentmesh.infrastructure.postgres.identity_repositories import SqlAlchemyIdentityRepository
from agentmesh.infrastructure.postgres.mcp_registry_repositories import (
    SqlAlchemyMcpRegistryRepository,
)
from agentmesh.infrastructure.postgres.planning_repositories import (
    SqlAlchemyGoalContractRepository,
    SqlAlchemyPlanPatchRepository,
)
from agentmesh.infrastructure.postgres.policy_repositories import SqlAlchemyPolicyRepository
from agentmesh.infrastructure.postgres.quota_repositories import SqlAlchemyQuotaRepository
from agentmesh.infrastructure.postgres.registry_repositories import (
    SqlAlchemyAgentDefinitionRepository,
    SqlAlchemyAgentDeploymentRepository,
    SqlAlchemyAgentInstanceRepository,
    SqlAlchemyAgentVersionRepository,
    SqlAlchemyCapabilityRepository,
)
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyHandoffRepository,
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyInboxRepository,
    SqlAlchemyOutboxRepository,
    SqlAlchemySubtaskDependencyRepository,
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskAttemptRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskResolutionRepository,
    SqlAlchemyTaskRunRepository,
    SqlAlchemyUsageRecordRepository,
)
from agentmesh.infrastructure.postgres.tool_repositories import (
    SqlAlchemyToolExecutionAuthorizationRepository,
    SqlAlchemyToolInvocationRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.tasks = SqlAlchemyTaskRepository(self._session)
        self.goal_contracts = SqlAlchemyGoalContractRepository(self._session)
        self.plan_patches = SqlAlchemyPlanPatchRepository(self._session)
        self.task_resolutions = SqlAlchemyTaskResolutionRepository(self._session)
        self.subtasks = SqlAlchemySubtaskRepository(self._session)
        self.subtask_dependencies = SqlAlchemySubtaskDependencyRepository(self._session)
        self.handoffs = SqlAlchemyHandoffRepository(self._session)
        self.runs = SqlAlchemyTaskRunRepository(self._session)
        self.attempts = SqlAlchemyTaskAttemptRepository(self._session)
        self.quotas = SqlAlchemyQuotaRepository(self._session)
        self.outbox = SqlAlchemyOutboxRepository(self._session)
        self.inbox = SqlAlchemyInboxRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
        self.agent_definitions = SqlAlchemyAgentDefinitionRepository(self._session)
        self.agent_versions = SqlAlchemyAgentVersionRepository(self._session)
        self.capabilities = SqlAlchemyCapabilityRepository(self._session)
        self.agent_deployments = SqlAlchemyAgentDeploymentRepository(self._session)
        self.agent_instances = SqlAlchemyAgentInstanceRepository(self._session)
        self.artifacts = SqlAlchemyArtifactRepository(self._session)
        self.artifact_versions = SqlAlchemyArtifactVersionRepository(self._session)
        self.tool_invocations = SqlAlchemyToolInvocationRepository(self._session)
        self.tool_execution_authorizations = SqlAlchemyToolExecutionAuthorizationRepository(
            self._session
        )
        self.usage_records = SqlAlchemyUsageRecordRepository(self._session)
        self.policy = SqlAlchemyPolicyRepository(self._session)
        self.identity = SqlAlchemyIdentityRepository(self._session)
        self.mcp_registry = SqlAlchemyMcpRegistryRepository(self._session)
        self.a2a_registry = SqlAlchemyA2ARegistryRepository(self._session)
        self.remote_correlations = SqlAlchemyRemoteTaskCorrelationRepository(self._session)
        self.credentials = SqlAlchemyCredentialRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()
        self._session.close()

    def commit(self) -> None:
        try:
            self._session.commit()
        except StaleDataError as exc:
            self._session.rollback()
            raise ConcurrentTaskUpdate("Task was modified by another transaction") from exc
        except SQLAlchemyError:
            self._session.rollback()
            raise

    def flush(self) -> None:
        self._session.flush()

    def rollback(self) -> None:
        self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
