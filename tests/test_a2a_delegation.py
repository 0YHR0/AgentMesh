import pytest

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus
from agentmesh.domain.a2a_registry import A2ATrustTier
from agentmesh.domain.errors import A2ADelegationConflict, A2ATransportFailure
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tasks import RunStatus, TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory, ScriptedA2AClient


def _principal(principal_id: str, role: Role) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id="test-tenant",
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="test",
    )


def _card(*, security_requirements=None) -> dict:
    card = {
        "name": "Remote Researcher",
        "description": "Completes bounded research tasks.",
        "supportedInterfaces": [
            {
                "url": "https://peer.example/a2a/v1",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "version": "1.0.0",
        "capabilities": {},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "research",
                "name": "Research",
                "description": "Collect evidence.",
                "tags": ["research"],
            }
        ],
    }
    if security_requirements is not None:
        card["securityRequirements"] = security_requirements
    return card


def _setup(client: ScriptedA2AClient):
    factory = InMemoryUnitOfWorkFactory()
    policy = PolicyApprovalService(uow_factory=factory, tenant_id="test-tenant", enabled=True)
    registry = A2ARegistryService(uow_factory=factory, tenant_id="test-tenant")
    peer = registry.register_peer(
        owner_id="platform",
        name="research-peer",
        discovery_url="https://peer.example/.well-known/agent-card.json",
        allowed_endpoint_hosts=["peer.example"],
        allowed_bindings=["HTTP+JSON"],
        trust_tier=A2ATrustTier.TRUSTED,
        actor="operator",
        idempotency_key="peer",
    )
    registry.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag=None,
        actor="operator",
        idempotency_key="card",
    )
    gates = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true",
    )
    tasks = TaskApplicationService(factory, "local-agent", "test-tenant", feature_gates=gates)
    task = tasks.create_task("Collect evidence", {"topic": "A2A"}, TaskExecutionMode.FEDERATED).task
    service = A2ADelegationService(
        uow_factory=factory,
        tenant_id="test-tenant",
        policy_service=policy,
        client=client,
    )
    return factory, policy, tasks, service, task, peer


def _permit(policy, service, task_id, peer_id, requester):
    intent = service.intent(task_id, peer_id)
    requested = policy.request_action(
        principal=requester,
        action_type=GovernedActionType.A2A_DELEGATE,
        resource_type="task",
        resource_id=task_id,
        arguments=intent.arguments,
    )
    approved = policy.decide(
        requested.action.approval_id,
        principal=_principal("approver", Role.APPROVER),
        outcome=ApprovalOutcome.APPROVE,
        reason="Target and payload reviewed",
    )
    return approved.action.permit_id


def test_remote_task_is_sent_once_polled_and_normalized() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {
                "task": {
                    "id": "remote/42",
                    "contextId": "ctx-1",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            }
        ],
        task_responses=[
            {
                "id": "remote/42",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "Evidence collected"}]}],
            }
        ],
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)

    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate",
    )
    assert correlation.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert correlation.remote_task_id == "remote/42"
    assert client.send_calls[0]["message"]["messageId"] == str(correlation.outbound_message_id)

    completed = service.reconcile(correlation.id)
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert completed.result["parts"] == [{"text": "Evidence collected"}]
    assert len(client.send_calls) == len(client.task_calls) == 1
    assert factory.store.tasks[task.id].status is TaskStatus.COMPLETED
    assert factory.store.runs[correlation.run_id].status is RunStatus.SUCCEEDED


def test_ambiguous_send_is_not_retried_automatically() -> None:
    client = ScriptedA2AClient(
        send_responses=[A2ATransportFailure("timeout", request_may_have_been_sent=True)]
    )
    _, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate",
    )

    assert correlation.status is RemoteCorrelationStatus.OUTCOME_UNKNOWN
    replay = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=None,
        idempotency_key="delegate",
    )
    assert replay.id == correlation.id
    assert len(client.send_calls) == 1
    with pytest.raises(A2ADelegationConflict):
        service.dispatch(correlation.id)
    with pytest.raises(A2ADelegationConflict):
        service.reconcile(correlation.id)


def test_direct_message_completes_without_remote_task_id() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {
                "message": {
                    "messageId": "reply-1",
                    "role": "ROLE_AGENT",
                    "parts": [{"data": {"answer": 42}, "mediaType": "application/json"}],
                }
            }
        ]
    )
    _, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate",
    )
    assert correlation.status is RemoteCorrelationStatus.COMPLETED
    assert correlation.remote_task_id is None
    assert correlation.result["parts"][0]["data"] == {"answer": 42}


def test_invalid_external_artifact_requires_intervention() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {
                "task": {
                    "id": "remote-unsafe",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"url": "https://files.example/result"}]}],
                }
            }
        ]
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate",
    )
    assert correlation.status is RemoteCorrelationStatus.INTERVENTION_REQUIRED
    assert correlation.result is None
    assert "invalid_remote_response" in correlation.error
    assert factory.store.tasks[task.id].status is TaskStatus.WAITING_REMOTE


def test_late_remote_completion_does_not_overwrite_local_cancellation() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {
                "task": {
                    "id": "remote-late",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            }
        ],
        task_responses=[
            {
                "id": "remote-late",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "late output"}]}],
            }
        ],
    )
    factory, policy, tasks, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate",
    )
    tasks.cancel_task(task.id)

    completed = service.reconcile(correlation.id)
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert completed.late_result
    assert completed.result["parts"] == [{"text": "late output"}]
    assert factory.store.tasks[task.id].status is TaskStatus.CANCELED
    assert factory.store.runs[correlation.run_id].status is RunStatus.CANCELED
