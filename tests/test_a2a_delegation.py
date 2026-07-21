from dataclasses import replace
from datetime import timedelta

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
from agentmesh.domain.resolutions import A2AOutcomeDecision, TaskResolutionAction
from agentmesh.domain.tasks import RunStatus, TaskExecutionMode, TaskStatus, utc_now
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


def _setup(client: ScriptedA2AClient, **service_options):
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
        **service_options,
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
    assert correlation.next_poll_at is not None
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


def test_operator_binds_unknown_a2a_send_then_normal_reconciliation_continues() -> None:
    client = ScriptedA2AClient(
        send_responses=[A2ATransportFailure("timeout", request_may_have_been_sent=True)],
        task_responses=[
            {
                "id": "remote-recovered",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "recovered result"}]}],
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
        idempotency_key="delegate-unknown-bind",
    )

    reconciled = service.reconcile_unknown_outcome(
        correlation.id,
        principal=requester,
        decision=A2AOutcomeDecision.REMOTE_TASK_BOUND,
        reason="Peer operator located the Task",
        evidence_reference="ticket://FED-42",
        evidence_digest="sha256:" + "d" * 64,
        remote_task_id="remote-recovered",
        idempotency_key="bind-remote-1",
    )
    replay = service.reconcile_unknown_outcome(
        correlation.id,
        principal=requester,
        decision=A2AOutcomeDecision.REMOTE_TASK_BOUND,
        reason="Peer operator located the Task",
        evidence_reference="ticket://FED-42",
        evidence_digest="sha256:" + "d" * 64,
        remote_task_id="remote-recovered",
        idempotency_key="bind-remote-1",
    )
    completed = service.reconcile(correlation.id)

    assert reconciled.correlation.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert reconciled.resolution.action is TaskResolutionAction.BIND_A2A_REMOTE_TASK
    assert replay.resolution.id == reconciled.resolution.id
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert completed.result["parts"] == [{"text": "recovered result"}]
    assert factory.store.tasks[task.id].status is TaskStatus.COMPLETED
    assert len(client.send_calls) == 1


def test_operator_confirms_unknown_a2a_send_was_not_delivered() -> None:
    client = ScriptedA2AClient(
        send_responses=[A2ATransportFailure("timeout", request_may_have_been_sent=True)]
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-not-delivered",
    )

    reconciled = service.reconcile_unknown_outcome(
        correlation.id,
        principal=requester,
        decision=A2AOutcomeDecision.NOT_DELIVERED,
        reason="Peer ingress logs confirm no request",
        evidence_reference="ticket://FED-43",
        evidence_digest="sha256:" + "e" * 64,
        idempotency_key="not-delivered-1",
    )

    assert reconciled.correlation.status is RemoteCorrelationStatus.FAILED
    assert reconciled.resolution.action is TaskResolutionAction.RECONCILE_A2A_NOT_DELIVERED
    assert factory.store.tasks[task.id].status is TaskStatus.FAILED
    assert factory.store.runs[correlation.run_id].status is RunStatus.FAILED
    assert len(client.send_calls) == 1


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


def test_due_reconciliation_reschedules_active_then_stops_at_terminal() -> None:
    client = ScriptedA2AClient(
        send_responses=[{"task": {"id": "remote-auto", "status": {"state": "TASK_STATE_WORKING"}}}],
        task_responses=[
            {"id": "remote-auto", "status": {"state": "TASK_STATE_WORKING"}},
            {
                "id": "remote-auto",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "automatic result"}]}],
            },
        ],
    )
    factory, policy, _, service, task, peer = _setup(client, poll_interval=timedelta(seconds=10))
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-auto",
    )
    factory.store.remote_correlations[correlation.id] = replace(
        correlation, next_poll_at=utc_now() - timedelta(seconds=1)
    )

    active = service.reconcile_due(worker_id="reconciler-1", limit=10)
    after_active = factory.store.remote_correlations[correlation.id]
    assert active.claimed == active.active == 1
    assert after_active.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert after_active.poll_count == 1
    assert after_active.poll_lease_owner is None
    assert after_active.next_poll_at is not None

    factory.store.remote_correlations[correlation.id] = replace(
        after_active, next_poll_at=utc_now() - timedelta(seconds=1)
    )
    terminal = service.reconcile_due(worker_id="reconciler-2", limit=10)
    completed = factory.store.remote_correlations[correlation.id]
    assert terminal.claimed == terminal.terminal == 1
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert completed.next_poll_at is None
    assert factory.store.tasks[task.id].status is TaskStatus.COMPLETED


def test_reconciliation_failures_back_off_then_require_intervention() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "remote-flaky", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        task_responses=[
            A2ATransportFailure("peer timeout", request_may_have_been_sent=True),
            A2ATransportFailure("peer timeout", request_may_have_been_sent=False),
        ],
    )
    factory, policy, _, service, task, peer = _setup(
        client,
        poll_failure_base_delay=timedelta(seconds=2),
        poll_failure_max_delay=timedelta(seconds=10),
        poll_max_failures=2,
    )
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-flaky",
    )
    factory.store.remote_correlations[correlation.id] = replace(
        correlation, next_poll_at=utc_now() - timedelta(seconds=1)
    )

    first = service.reconcile_due(worker_id="reconciler-1", limit=1)
    retrying = factory.store.remote_correlations[correlation.id]
    assert first.failed_polls == 1
    assert retrying.poll_failure_count == 1
    assert retrying.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert retrying.next_poll_at is not None and retrying.next_poll_at > utc_now()

    factory.store.remote_correlations[correlation.id] = replace(
        retrying, next_poll_at=utc_now() - timedelta(seconds=1)
    )
    second = service.reconcile_due(worker_id="reconciler-2", limit=1)
    exhausted = factory.store.remote_correlations[correlation.id]
    assert second.failed_polls == second.intervention_required == 1
    assert exhausted.status is RemoteCorrelationStatus.INTERVENTION_REQUIRED
    assert exhausted.next_poll_at is None
    assert exhausted.poll_lease_owner is None
    assert "reconcile_exhausted" in exhausted.error
    assert factory.store.tasks[task.id].status is TaskStatus.WAITING_REMOTE


def test_poll_claim_blocks_concurrent_worker_until_lease_expires() -> None:
    client = ScriptedA2AClient(
        send_responses=[{"task": {"id": "remote-lease", "status": {"state": "TASK_STATE_WORKING"}}}]
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-lease",
    )
    now = utc_now()
    factory.store.remote_correlations[correlation.id] = replace(
        correlation, next_poll_at=now - timedelta(seconds=1)
    )
    with factory() as uow:
        first = uow.remote_correlations.claim_due(
            tenant_id="test-tenant",
            now=now,
            owner="worker-1",
            lease_expires_at=now + timedelta(seconds=30),
            limit=1,
        )
        uow.commit()
    with factory() as uow:
        blocked = uow.remote_correlations.claim_due(
            tenant_id="test-tenant",
            now=now + timedelta(seconds=1),
            owner="worker-2",
            lease_expires_at=now + timedelta(seconds=31),
            limit=1,
        )
        recovered = uow.remote_correlations.claim_due(
            tenant_id="test-tenant",
            now=now + timedelta(seconds=31),
            owner="worker-2",
            lease_expires_at=now + timedelta(seconds=61),
            limit=1,
        )
    assert len(first) == 1
    assert blocked == []
    assert len(recovered) == 1
    assert recovered[0].poll_lease_owner == "worker-2"


def test_remote_cancellation_is_idempotent_and_converges_local_state() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "remote-cancel", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        cancel_responses=[
            {"id": "remote-cancel", "status": {"state": "TASK_STATE_CANCELED"}}
        ],
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-cancel",
    )

    canceled = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-1",
        reason="No longer needed",
    )
    replay = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-1",
        reason="No longer needed",
    )

    assert canceled.status is replay.status is RemoteCorrelationStatus.CANCELED
    assert canceled.cancel_request_count == 1
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["metadata"]["reason"] == "No longer needed"
    assert factory.store.tasks[task.id].status is TaskStatus.CANCELED
    assert factory.store.runs[correlation.run_id].status is RunStatus.CANCELED


def test_remote_cancellation_lost_race_preserves_actual_completion() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "remote-race", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        cancel_responses=[
            {"id": "remote-race", "status": {"state": "TASK_STATE_WORKING"}}
        ],
        task_responses=[
            {
                "id": "remote-race",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "finished first"}]}],
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
        idempotency_key="delegate-race",
    )

    pending = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-race",
        reason="Stop if possible",
    )
    completed = service.reconcile(pending.id)

    assert pending.status is RemoteCorrelationStatus.CANCEL_PENDING
    assert factory.store.tasks[task.id].status is TaskStatus.COMPLETED
    assert completed.status is RemoteCorrelationStatus.COMPLETED
    assert completed.result["parts"] == [{"text": "finished first"}]


def test_ambiguous_cancellation_is_polled_without_resending() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "remote-unknown", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        cancel_responses=[
            A2ATransportFailure("timeout", request_may_have_been_sent=True)
        ],
        task_responses=[
            {"id": "remote-unknown", "status": {"state": "TASK_STATE_CANCELED"}}
        ],
    )
    factory, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-unknown",
    )

    unknown = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-unknown",
        reason="Stop",
    )
    repeated = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-unknown-new-key",
        reason="Stop",
    )
    canceled = service.reconcile(unknown.id)

    assert unknown.status is repeated.status is RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN
    assert len(client.cancel_calls) == 1
    assert canceled.status is RemoteCorrelationStatus.CANCELED
    assert factory.store.tasks[task.id].status is TaskStatus.CANCELED


def test_cancellation_failure_before_send_allows_explicit_new_attempt() -> None:
    client = ScriptedA2AClient(
        send_responses=[
            {"task": {"id": "remote-retry", "status": {"state": "TASK_STATE_WORKING"}}}
        ],
        cancel_responses=[
            A2ATransportFailure("connect failed", request_may_have_been_sent=False),
            {"id": "remote-retry", "status": {"state": "TASK_STATE_CANCELED"}},
        ],
        task_responses=[
            {"id": "remote-retry", "status": {"state": "TASK_STATE_WORKING"}}
        ],
    )
    _, policy, _, service, task, peer = _setup(client)
    requester = _principal("operator", Role.FEDERATION_OPERATOR)
    correlation = service.delegate(
        task.id,
        peer.id,
        principal=requester,
        permit_id=_permit(policy, service, task.id, peer.id, requester),
        idempotency_key="delegate-retry",
    )

    retryable = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-retry-1",
        reason="Stop",
    )
    replay = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-retry-1",
        reason="Stop",
    )
    still_waiting = service.reconcile(retryable.id)
    canceled = service.cancel(
        correlation.id,
        principal=requester,
        idempotency_key="cancel-retry-2",
        reason="Stop",
    )

    assert retryable.status is replay.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert still_waiting.status is RemoteCorrelationStatus.WAITING_REMOTE
    assert canceled.status is RemoteCorrelationStatus.CANCELED
    assert canceled.cancel_request_count == 2
    assert len(client.cancel_calls) == 2
