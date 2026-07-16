import pytest

from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.domain.errors import AgentRegistryConflict, InvalidAgentTransition
from agentmesh.domain.registry import (
    AgentVersionStatus,
    AgentVisibility,
    DeploymentStatus,
    InstanceHealth,
)


def create_published_reviewer(service: AgentRegistryService):
    service.create_capability(
        key="code.review.python",
        version="1.0.0",
        description="Review Python code",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        evidence_requirements=["contract-test"],
    )
    definition = service.create_definition(
        owner_id="platform-team",
        name="python-reviewer",
        description="Reviews Python changes",
        visibility=AgentVisibility.TENANT,
        tags=["python", "review"],
    )
    version = service.create_version(
        definition.definition.id,
        semantic_version="1.0.0",
        role="Python reviewer",
        instructions="Review Python code.",
        declared_capabilities=["code.review.python"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        runtime_adapter="local-python",
        execution_modes=["async"],
    )
    service.submit_version(version.id)
    published = service.publish_version(
        version.id,
        verified_capabilities=["code.review.python"],
        make_default=True,
    )
    return definition.definition, published


def test_registry_publication_candidate_and_revocation(
    registry_service: AgentRegistryService,
) -> None:
    definition, version = create_published_reviewer(registry_service)

    assert version.status == AgentVersionStatus.PUBLISHED
    assert version.content_digest is not None
    assert (
        registry_service.get_definition(definition.id).definition.default_version_id == version.id
    )

    candidates = registry_service.find_candidates(
        required_capabilities=["code.review.python"], execution_mode="async"
    )
    assert [candidate.agent_version.id for candidate in candidates] == [version.id]

    revoked = registry_service.revoke_version(version.id, reason="compromised artifact")
    assert revoked.status == AgentVersionStatus.REVOKED
    assert registry_service.get_definition(definition.id).definition.default_version_id is None
    assert registry_service.find_candidates(required_capabilities=["code.review.python"]) == []


def test_duplicate_agent_name_is_rejected(registry_service: AgentRegistryService) -> None:
    registry_service.create_definition(
        owner_id="one",
        name="duplicate-agent",
        description="first",
        visibility=AgentVisibility.PRIVATE,
        tags=[],
    )

    with pytest.raises(AgentRegistryConflict):
        registry_service.create_definition(
            owner_id="two",
            name="duplicate-agent",
            description="second",
            visibility=AgentVisibility.PRIVATE,
            tags=[],
        )


def test_archived_definition_is_not_a_candidate(
    registry_service: AgentRegistryService,
) -> None:
    definition, _version = create_published_reviewer(registry_service)

    registry_service.archive_definition(definition.id)

    assert registry_service.find_candidates(required_capabilities=["code.review.python"]) == []


def test_deployment_and_instance_heartbeat(registry_service: AgentRegistryService) -> None:
    _definition, version = create_published_reviewer(registry_service)
    deployment = registry_service.create_deployment(
        version.id,
        environment="test",
        runtime_kind="local-process",
        endpoint_reference=None,
        remote_peer_id=None,
        traffic_weight=100,
        region="local",
        rollout_policy={},
    )
    deployment = registry_service.update_deployment_status(
        deployment.id,
        desired_status=DeploymentStatus.ACTIVE,
        current_status=DeploymentStatus.ACTIVE,
    )
    assert deployment.current_status == DeploymentStatus.ACTIVE

    instance = registry_service.heartbeat_instance(
        deployment.id,
        external_instance_id="worker-1",
        health=InstanceHealth.HEALTHY,
        capacity_slots=4,
        active_slots=1,
        protocol_endpoint=None,
        lease_epoch=1,
        metadata={"zone": "local"},
    )
    assert instance.health == InstanceHealth.HEALTHY
    assert registry_service.list_instances(deployment.id)[0].active_slots == 1

    with pytest.raises(InvalidAgentTransition):
        registry_service.heartbeat_instance(
            deployment.id,
            external_instance_id="worker-1",
            health=InstanceHealth.HEALTHY,
            capacity_slots=4,
            active_slots=0,
            protocol_endpoint=None,
            lease_epoch=0,
            metadata={},
        )
