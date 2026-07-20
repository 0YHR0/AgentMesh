from datetime import timedelta

import pytest

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus
from agentmesh.domain.a2a_registry import A2ATrustTier
from agentmesh.domain.credentials import (
    CredentialBindingStatus,
    CredentialLeaseStatus,
    SecretProvider,
    SecretPurpose,
    SecretReferenceStatus,
)
from agentmesh.domain.errors import CredentialConflict, CredentialProviderUnavailable
from agentmesh.domain.identity import (
    Principal,
    PrincipalContext,
    PrincipalType,
    Role,
)
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tasks import Task, TaskExecutionMode, TaskRun, utc_now
from tests.fakes import InMemoryUnitOfWorkFactory, ScriptedA2AClient


class StaticProvider:
    def __init__(self, value: str = "peer-token-value") -> None:
        self.value = value
        self.calls = 0
        self.on_resolve = None

    def resolve(self, reference) -> str:
        self.calls += 1
        if self.on_resolve is not None:
            self.on_resolve()
        if not self.value:
            raise CredentialProviderUnavailable("unavailable")
        return self.value


def _principal(principal_id: str, role: Role) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id="test-tenant",
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="test",
    )


def _setup(provider: StaticProvider | None = None):
    factory = InMemoryUnitOfWorkFactory()
    workload = Principal.create(
        principal_id=None,
        tenant_id="test-tenant",
        principal_type=PrincipalType.SERVICE,
        display_name="A2A Gateway",
    )
    factory.store.principals[workload.id] = workload
    registry = A2ARegistryService(uow_factory=factory, tenant_id="test-tenant")
    peer = registry.register_peer(
        owner_id="platform",
        name="secured-peer",
        discovery_url="https://peer.example/.well-known/agent-card.json",
        allowed_endpoint_hosts=["peer.example"],
        allowed_bindings=["HTTP+JSON"],
        trust_tier=A2ATrustTier.TRUSTED,
        actor="admin",
        idempotency_key="peer",
    )
    snapshot = registry.import_card(
        peer.id,
        card={
            "name": "Secured Peer",
            "description": "Requires workload Bearer authentication.",
            "supportedInterfaces": [
                {
                    "url": "https://peer.example/a2a/v1",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                }
            ],
            "version": "1.0.0",
            "capabilities": {},
            "securitySchemes": {
                "workloadBearer": {
                    "httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "opaque"}
                }
            },
            "securityRequirements": [{"workloadBearer": ["a2a.tasks"]}],
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "research",
                    "name": "Research",
                    "description": "Collect evidence.",
                    "tags": ["research"],
                }
            ],
        },
        ttl_seconds=3600,
        source_etag=None,
        actor="admin",
        idempotency_key="card",
    )
    policy = PolicyApprovalService(uow_factory=factory, tenant_id="test-tenant", enabled=True)
    broker = CredentialBrokerService(
        uow_factory=factory,
        tenant_id="test-tenant",
        policy_service=policy,
        provider=provider or StaticProvider(),
        environment="test",
    )
    return factory, workload, peer, snapshot, policy, broker


def _binding(factory, workload, peer, policy, broker):
    admin = _principal("admin", Role.TENANT_ADMIN)
    reference = broker.create_secret_reference(
        provider=SecretProvider.ENVIRONMENT,
        external_key="AGENTMESH_TEST_PEER_TOKEN",
        version_selector=None,
        purpose=SecretPurpose.A2A_HTTP_BEARER,
        allowed_audiences=("https://peer.example/a2a/v1",),
        principal=admin,
        idempotency_key="reference",
    )
    expires_at = utc_now() + timedelta(hours=1)
    intent = broker.binding_intent(
        workload_principal_id=workload.id,
        peer_id=peer.id,
        secret_reference_id=reference.id,
        environment="test",
        expires_at=expires_at,
    )
    requested = policy.request_action(
        principal=admin,
        action_type=GovernedActionType.CREDENTIAL_BINDING_CREATE,
        resource_type="a2a_peer",
        resource_id=peer.id,
        arguments=intent.arguments,
    )
    approved = policy.decide(
        requested.action.approval_id,
        principal=_principal("approver", Role.APPROVER),
        outcome=ApprovalOutcome.APPROVE,
        reason="Workload, audience, scope, and reference reviewed",
    )
    binding = broker.create_binding(
        workload_principal_id=workload.id,
        peer_id=peer.id,
        secret_reference_id=reference.id,
        environment="test",
        expires_at=expires_at,
        principal=admin,
        permit_id=approved.action.permit_id,
        idempotency_key="binding",
    )
    return reference, binding


def test_reference_binding_and_lease_never_persist_secret_value() -> None:
    provider = StaticProvider()
    factory, workload, peer, snapshot, policy, broker = _setup(provider)
    reference, binding = _binding(factory, workload, peer, policy, broker)
    task = Task.create(
        tenant_id="test-tenant",
        objective="Secured delegation",
        input={},
        execution_mode=TaskExecutionMode.FEDERATED,
    )
    run = TaskRun.request(task_id=task.id, agent_id="a2a:secured-peer")
    run.wait_for_remote()
    task.queue_remote(run.id)
    factory.store.tasks[task.id] = task
    factory.store.runs[run.id] = run

    grant = broker.acquire_for_a2a(
        binding.id,
        workload_principal_id=workload.id,
        peer_id=peer.id,
        card_snapshot_id=snapshot.id,
        card_digest=snapshot.digest,
        audience="https://peer.example/a2a/v1",
        scheme_name="workloadBearer",
        scopes=("a2a.tasks",),
        task_id=task.id,
        run_id=run.id,
    )
    assert grant.material.value == "peer-token-value"
    assert "peer-token-value" not in repr(grant)
    assert provider.calls == 1
    assert factory.store.credential_leases[grant.lease.id].status is CredentialLeaseStatus.ISSUED
    assert not hasattr(factory.store.credential_leases[grant.lease.id], "value")

    settled = broker.settle_lease(grant.lease.id, used=True)
    assert settled.status is CredentialLeaseStatus.USED
    assert reference.status is SecretReferenceStatus.ACTIVE
    assert binding.status is CredentialBindingStatus.ACTIVE


def test_revocation_and_provider_failure_fail_closed_with_audit_metadata() -> None:
    provider = StaticProvider(value="")
    factory, workload, peer, snapshot, policy, broker = _setup(provider)
    reference, binding = _binding(factory, workload, peer, policy, broker)
    task = Task.create(
        tenant_id="test-tenant",
        objective="Secured delegation",
        input={},
        execution_mode=TaskExecutionMode.FEDERATED,
    )
    run = TaskRun.request(task_id=task.id, agent_id="a2a:secured-peer")
    factory.store.tasks[task.id] = task
    factory.store.runs[run.id] = run
    request = dict(
        workload_principal_id=workload.id,
        peer_id=peer.id,
        card_snapshot_id=snapshot.id,
        card_digest=snapshot.digest,
        audience="https://peer.example/a2a/v1",
        scheme_name="workloadBearer",
        scopes=("a2a.tasks",),
        task_id=task.id,
        run_id=run.id,
    )

    with pytest.raises(CredentialProviderUnavailable):
        broker.acquire_for_a2a(binding.id, **request)
    lease = next(iter(factory.store.credential_leases.values()))
    assert lease.status is CredentialLeaseStatus.FAILED
    assert lease.error == "provider_unavailable"

    broker.revoke_secret_reference(reference.id)
    with pytest.raises(CredentialConflict, match="inactive"):
        broker.acquire_for_a2a(binding.id, **request)


def test_revocation_during_resolution_prevents_lease_issuance() -> None:
    provider = StaticProvider()
    factory, workload, peer, snapshot, policy, broker = _setup(provider)
    reference, binding = _binding(factory, workload, peer, policy, broker)
    task = Task.create(
        tenant_id="test-tenant",
        objective="Race revocation against credential resolution",
        input={},
        execution_mode=TaskExecutionMode.FEDERATED,
    )
    run = TaskRun.request(task_id=task.id, agent_id="a2a:secured-peer")
    factory.store.tasks[task.id] = task
    factory.store.runs[run.id] = run
    provider.on_resolve = lambda: broker.revoke_secret_reference(reference.id)

    with pytest.raises(CredentialConflict, match="inactive"):
        broker.acquire_for_a2a(
            binding.id,
            workload_principal_id=workload.id,
            peer_id=peer.id,
            card_snapshot_id=snapshot.id,
            card_digest=snapshot.digest,
            audience="https://peer.example/a2a/v1",
            scheme_name="workloadBearer",
            scopes=("a2a.tasks",),
            task_id=task.id,
            run_id=run.id,
        )
    lease = next(iter(factory.store.credential_leases.values()))
    assert lease.status is CredentialLeaseStatus.FAILED
    assert lease.error == "binding_changed_before_issuance"


def test_authenticated_a2a_send_and_poll_use_fresh_brokered_leases() -> None:
    provider = StaticProvider()
    factory, workload, peer, _, policy, broker = _setup(provider)
    _, binding = _binding(factory, workload, peer, policy, broker)
    task = Task.create(
        tenant_id="test-tenant",
        objective="Secured delegation",
        input={"classification": "internal"},
        execution_mode=TaskExecutionMode.FEDERATED,
    )
    factory.store.tasks[task.id] = task
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "secured-remote", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        task_responses=[
            {
                "id": "secured-remote",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "secured result"}]}],
            }
        ],
    )
    delegation = A2ADelegationService(
        uow_factory=factory,
        tenant_id="test-tenant",
        policy_service=policy,
        client=client,
        credential_broker=broker,
        workload_principal_id=workload.id,
    )
    operator = _principal("federation-operator", Role.FEDERATION_OPERATOR)
    intent = delegation.intent(task.id, peer.id, binding.id)
    requested = policy.request_action(
        principal=operator,
        action_type=GovernedActionType.A2A_DELEGATE,
        resource_type="task",
        resource_id=task.id,
        arguments=intent.arguments,
    )
    approved = policy.decide(
        requested.action.approval_id,
        principal=_principal("delegation-approver", Role.APPROVER),
        outcome=ApprovalOutcome.APPROVE,
        reason="Approved secured delegation",
    )
    correlation = delegation.delegate(
        task.id,
        peer.id,
        principal=operator,
        permit_id=approved.action.permit_id,
        idempotency_key="secured-delegation",
        credential_binding_id=binding.id,
    )
    assert correlation.status is RemoteCorrelationStatus.WAITING_REMOTE
    first_material = client.send_calls[0]["credential"]
    assert first_material.value == "peer-token-value"
    assert correlation.last_credential_lease_id == first_material.lease_id

    completed = delegation.reconcile(correlation.id)
    second_material = client.task_calls[0]["credential"]
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert first_material.lease_id != second_material.lease_id
    assert completed.last_credential_lease_id == second_material.lease_id
    assert provider.calls == 2
    assert {lease.status for lease in factory.store.credential_leases.values()} == {
        CredentialLeaseStatus.USED
    }
