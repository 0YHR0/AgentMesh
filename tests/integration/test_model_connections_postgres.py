"""Persistence and publish-boundary checks for tenant model connections."""

import os
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.model_connection_services import ModelConnectionService
from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.config import get_settings
from agentmesh.domain.errors import (
    AgentRegistryConflict,
    InvalidAgentTransition,
    InvalidAgentVersion,
)
from agentmesh.domain.model_runtime import ModelRuntimePolicy
from agentmesh.domain.registry import AgentVisibility
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.model_agent import ModelProviderError, VersionBoundAgentExecutor

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_model_connection_secret_and_published_snapshot_survive_postgres_restart() -> None:
    tenant_id = f"model-connection-{uuid4().hex}"
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    key = Fernet.generate_key().decode()
    connections = ModelConnectionService(
        uow_factory=factory, tenant_id=tenant_id, encryption_key=key
    )
    try:
        created = connections.create(
            {
                "name": "deepseek-" + tenant_id,
                "provider": "deepseek",
                "credential": {"type": "api_key", "value": "db-only-test-key"},
            }
        )
        connection_id = UUID(created["id"])

        # A fresh service instance models another API or worker process restart.
        restarted = ModelConnectionService(
            uow_factory=factory, tenant_id=tenant_id, encryption_key=key
        )
        stored = restarted.get(connection_id)
        assert stored is not None
        assert stored.encrypted_secret is not None
        assert b"db-only-test-key" not in stored.encrypted_secret
        assert restarted.resolve_secret(stored) == "db-only-test-key"
        with pytest.raises(InvalidAgentVersion, match="cannot be decrypted"):
            ModelConnectionService(
                uow_factory=factory,
                tenant_id=tenant_id,
                encryption_key=Fernet.generate_key().decode(),
            ).resolve_secret(stored)

        registry = AgentRegistryService(uow_factory=factory, tenant_id=tenant_id)
        capability = "model.connection." + uuid4().hex
        registry.create_capability(
            key=capability,
            version="1.0.0",
            description="Use a configured model connection",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            evidence_requirements=["contract-test"],
        )
        definition = registry.create_definition(
            owner_id="integration",
            name="model-runner-" + tenant_id,
            description="postgres publish snapshot integration",
            visibility=AgentVisibility.TENANT,
            tags=[],
        )
        version = registry.create_version(
            definition.definition.id,
            semantic_version="1.0.0",
            role="executor",
            instructions="Return a concise result.",
            declared_capabilities=[capability],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            model_policy={
                "provider": "deepseek",
                "model": "deepseek-flash",
                "max_output_tokens": 256,
                "connection_id": str(connection_id),
            },
            execution_modes=["async"],
        )
        registry.submit_version(version.id)
        published = registry.publish_version(
            version.id, verified_capabilities=[capability], make_default=True
        )
        digest = published.content_digest
        snapshot = published.model_policy["connection_snapshot"]
        assert snapshot == {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "revision": 1,
        }

        # A later edit/rotation must not redirect this immutable published version.
        restarted.update(
            connection_id,
            {
                "model": "deepseek-v4-pro",
                "endpoint": "https://api.deepseek.com/v1/chat/completions",
                "credential": {"type": "api_key", "value": "rotated-test-key"},
            },
        )
        persisted = registry.get_definition(definition.definition.id).versions[0]
        assert persisted.content_digest == digest
        assert persisted._calculate_digest() == digest
        assert ModelRuntimePolicy.from_dict(persisted.model_policy).connection_snapshot == snapshot
        with pytest.raises(InvalidAgentTransition):
            registry.publish_version(
                version.id, verified_capabilities=[capability], make_default=True
            )
        with pytest.raises(AgentRegistryConflict):
            restarted.create(
                {
                    "name": "deepseek-" + tenant_id,
                    "provider": "deepseek",
                    "credential": {"type": "api_key", "value": "other-test-key"},
                }
            )
        second = restarted.create(
            {
                "name": "secondary-" + tenant_id,
                "provider": "deepseek",
                "credential": {"type": "api_key", "value": "another-test-key"},
            }
        )
        with pytest.raises(AgentRegistryConflict):
            restarted.update(UUID(second["id"]), {"name": "deepseek-" + tenant_id})

        restarted.disable(connection_id)
        executor = VersionBoundAgentExecutor(
            uow_factory=factory,
            fallback=DeterministicAgentExecutor(),
            model_connection_service=restarted,
            identity_rbac_enabled=True,
        )
        context = AgentExecutionContext(
            task_id=uuid4(),
            run_id=uuid4(),
            thread_id="integration",
            agent_id="integration-agent",
            agent_version_id=version.id,
            agent_version_digest=digest,
            tenant_id=tenant_id,
        )
        with pytest.raises(ModelProviderError, match="disabled"):
            executor.execute(objective="no provider call", input={}, context=context)
    finally:
        engine.dispose()
