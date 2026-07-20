from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.credential_services import (
    A2ABearerRequirement,
    CredentialBrokerService,
    bearer_requirement,
)
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.ports import A2AProtocolClient, UnitOfWorkFactory
from agentmesh.domain.a2a_delegation import (
    RemoteCorrelationStatus,
    RemoteTaskCorrelation,
)
from agentmesh.domain.a2a_registry import A2AEndpoint, A2APeer, A2APeerStatus, AgentCardSnapshot
from agentmesh.domain.errors import (
    A2ADelegationConflict,
    A2ADelegationNotFound,
    A2ATransportFailure,
    CredentialConflict,
    CredentialNotFound,
    CredentialProviderUnavailable,
    IdempotencyConflict,
    InvalidA2ADelegation,
    InvalidCredential,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.policy import GovernedActionType
from agentmesh.domain.tasks import (
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    Task,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

REMOTE_ACTIVE_STATES = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
REMOTE_INTERVENTION_STATES = {
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
    "TASK_STATE_UNSPECIFIED",
}


@dataclass(frozen=True)
class DelegationIntent:
    task_id: UUID
    peer_id: UUID
    arguments: dict[str, Any]


class A2ADelegationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        policy_service: PolicyApprovalService,
        client: A2AProtocolClient,
        credential_broker: CredentialBrokerService | None = None,
        workload_principal_id: UUID | None = None,
        max_inline_result_bytes: int = 65_536,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._policy = policy_service
        self._client = client
        self._credential_broker = credential_broker
        self._workload_principal_id = workload_principal_id
        self._max_inline_result_bytes = max_inline_result_bytes

    def intent(
        self,
        task_id: UUID,
        peer_id: UUID,
        credential_binding_id: UUID | None = None,
    ) -> DelegationIntent:
        with self._uow_factory() as uow:
            task = self._task_or_raise(uow, task_id)
            if task.execution_mode is not TaskExecutionMode.FEDERATED:
                raise InvalidA2ADelegation("Only FEDERATED Tasks can be delegated over A2A")
            if task.status is not TaskStatus.CREATED:
                raise A2ADelegationConflict("Federated Task is no longer available for delegation")
            if uow.remote_correlations.get_for_task(task.id) is not None:
                raise A2ADelegationConflict("Task already has a remote correlation")
            peer, snapshot, endpoint, requirement = self._resolve_target(uow, peer_id)
            arguments = self._policy_arguments(
                task,
                peer,
                snapshot,
                endpoint,
                requirement=requirement,
                credential_binding_id=credential_binding_id,
            )
            return DelegationIntent(task.id, peer.id, arguments)

    def delegate(
        self,
        task_id: UUID,
        peer_id: UUID,
        *,
        principal: PrincipalContext,
        permit_id: UUID | None,
        idempotency_key: str,
        credential_binding_id: UUID | None = None,
    ) -> RemoteTaskCorrelation:
        self._require_principal(principal)
        if not self._policy.enabled:
            raise InvalidA2ADelegation("A2A delegation requires the Policy service")
        scope = f"a2a-delegation:{self._tenant_id}:{principal.principal_id}"
        with self._uow_factory() as uow:
            existing = uow.remote_correlations.get_for_task(task_id)
            if existing is not None:
                if existing.tenant_id != self._tenant_id or existing.peer_id != peer_id:
                    raise A2ADelegationConflict("Task is already bound to another A2A Peer")
                return existing
            task = self._task_or_raise(uow, task_id)
            peer, snapshot, endpoint, requirement = self._resolve_target(uow, peer_id)
            arguments = self._policy_arguments(
                task,
                peer,
                snapshot,
                endpoint,
                requirement=requirement,
                credential_binding_id=credential_binding_id,
            )
            request_hash = _digest({"task_id": str(task_id), "arguments": arguments}).removeprefix(
                "sha256:"
            )
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                correlation = uow.remote_correlations.get(UUID(replay["correlation_id"]))
                if correlation is None or correlation.tenant_id != self._tenant_id:
                    raise A2ADelegationConflict("A2A delegation idempotency result was lost")
                return correlation

        self._policy.consume_permit(
            permit_id,
            principal=principal,
            action_type=GovernedActionType.A2A_DELEGATE,
            resource_type="task",
            resource_id=task_id,
            arguments=arguments,
        )

        with self._uow_factory() as uow:
            task = self._task_or_raise(uow, task_id, for_update=True)
            if uow.remote_correlations.get_for_task(task.id) is not None:
                raise A2ADelegationConflict("Task was delegated concurrently")
            current_peer, current_snapshot, current_endpoint, current_requirement = (
                self._resolve_target(uow, peer_id)
            )
            current_arguments = self._policy_arguments(
                task,
                current_peer,
                current_snapshot,
                current_endpoint,
                requirement=current_requirement,
                credential_binding_id=credential_binding_id,
            )
            if current_arguments != arguments:
                raise A2ADelegationConflict(
                    "A2A delegation target changed after approval; request a new Permit"
                )
            run = TaskRun.request(
                task_id=task.id,
                agent_id=f"a2a:{current_peer.name}:{current_snapshot.agent_name}"[:128],
            )
            run.wait_for_remote()
            task.queue_remote(run.id)
            correlation = RemoteTaskCorrelation.prepare(
                tenant_id=self._tenant_id,
                task_id=task.id,
                run_id=run.id,
                peer_id=current_peer.id,
                card_snapshot_id=current_snapshot.id,
                card_digest=current_snapshot.digest,
                endpoint_url=current_endpoint.url,
                protocol_binding=current_endpoint.protocol_binding,
                protocol_version=current_endpoint.protocol_version,
                endpoint_tenant=current_endpoint.tenant,
                outbound_message_id=UUID(arguments["outbound_message_id"]),
                request_digest=arguments["task_digest"],
                credential_binding_id=(
                    UUID(arguments["credential_binding_id"])
                    if arguments["credential_binding_id"]
                    else None
                ),
                credential_scheme_name=arguments["credential_scheme_name"],
                credential_scopes=tuple(arguments["credential_scopes"]),
            )
            uow.runs.add(run)
            uow.tasks.save(task)
            uow.remote_correlations.add(correlation)
            self._record(
                uow,
                scope,
                idempotency_key,
                request_hash,
                {"correlation_id": str(correlation.id)},
            )
            self._event(uow, correlation, "agentmesh.a2a.delegation-prepared")
            uow.commit()
        return self.dispatch(correlation.id)

    def dispatch(self, correlation_id: UUID) -> RemoteTaskCorrelation:
        with self._uow_factory() as uow:
            correlation = self._correlation_or_raise(uow, correlation_id, for_update=True)
            if correlation.status is not RemoteCorrelationStatus.PREPARED:
                raise A2ADelegationConflict(
                    f"Correlation cannot be sent from {correlation.status.value}"
                )
            task = self._task_or_raise(uow, correlation.task_id)
            snapshot = uow.a2a_registry.get_snapshot(correlation.card_snapshot_id)
            if snapshot is None or snapshot.digest != correlation.card_digest:
                raise A2ADelegationConflict("Bound A2A Card snapshot is unavailable")
            sending = correlation.mark_sending()
            uow.remote_correlations.save(sending)
            self._event(uow, sending, "agentmesh.a2a.delegation-sending")
            uow.commit()
        try:
            grant = self._acquire_credential(sending)
        except (
            CredentialConflict,
            CredentialNotFound,
            CredentialProviderUnavailable,
            InvalidA2ADelegation,
            InvalidCredential,
        ):
            return self._transport_failure(
                sending.id,
                A2ATransportFailure(
                    "A2A credential acquisition failed",
                    request_may_have_been_sent=False,
                ),
            )
        try:
            response = self._client.send_message(
                endpoint_url=sending.endpoint_url,
                protocol_version=sending.protocol_version,
                endpoint_tenant=sending.endpoint_tenant,
                message=self._message(task, sending, snapshot),
                accepted_output_modes=("application/json", "text/plain"),
                credential=grant.material if grant else None,
            )
        except A2ATransportFailure as exc:
            return self._transport_failure(sending.id, exc)
        finally:
            if grant is not None:
                assert self._credential_broker is not None
                self._credential_broker.settle_lease(grant.lease.id, used=True)
        try:
            return self._apply_response(sending.id, response, from_poll=False)
        except InvalidA2ADelegation as exc:
            return self._invalid_response(sending.id, response, str(exc), from_poll=False)

    def reconcile(self, correlation_id: UUID) -> RemoteTaskCorrelation:
        with self._uow_factory() as uow:
            correlation = self._correlation_or_raise(uow, correlation_id)
            if correlation.status not in {
                RemoteCorrelationStatus.WAITING_REMOTE,
                RemoteCorrelationStatus.INTERVENTION_REQUIRED,
            }:
                raise A2ADelegationConflict(
                    f"Correlation cannot be polled from {correlation.status.value}"
                )
            if correlation.remote_task_id is None:
                raise A2ADelegationConflict("Correlation has no known remote Task ID")
        try:
            grant = self._acquire_credential(correlation)
        except (
            CredentialConflict,
            CredentialNotFound,
            CredentialProviderUnavailable,
            InvalidA2ADelegation,
            InvalidCredential,
        ) as exc:
            raise InvalidA2ADelegation("A2A poll credential acquisition failed") from exc
        try:
            response = self._client.get_task(
                endpoint_url=correlation.endpoint_url,
                protocol_version=correlation.protocol_version,
                endpoint_tenant=correlation.endpoint_tenant,
                remote_task_id=correlation.remote_task_id,
                credential=grant.material if grant else None,
            )
        except A2ATransportFailure as exc:
            if exc.request_may_have_been_sent:
                raise A2ADelegationConflict("A2A poll result is unavailable") from exc
            raise InvalidA2ADelegation("A2A poll failed before reaching the Peer") from exc
        finally:
            if grant is not None:
                assert self._credential_broker is not None
                self._credential_broker.settle_lease(grant.lease.id, used=True)
        try:
            return self._apply_response(correlation.id, response, from_poll=True)
        except InvalidA2ADelegation as exc:
            return self._invalid_response(correlation.id, response, str(exc), from_poll=True)

    def get(self, correlation_id: UUID) -> RemoteTaskCorrelation:
        with self._uow_factory() as uow:
            return self._correlation_or_raise(uow, correlation_id)

    def list(self, *, limit: int, offset: int) -> list[RemoteTaskCorrelation]:
        with self._uow_factory() as uow:
            return uow.remote_correlations.list(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def _apply_response(
        self,
        correlation_id: UUID,
        response: dict[str, Any],
        *,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        response_digest = _digest(response)
        if from_poll:
            remote_task = response
            direct_message = None
        else:
            remote_task = response.get("task")
            direct_message = response.get("message")
            if (remote_task is None) == (direct_message is None):
                raise InvalidA2ADelegation(
                    "A2A SendMessage response must contain exactly one task or message"
                )
        with self._uow_factory() as uow:
            correlation = self._correlation_or_raise(uow, correlation_id, for_update=True)
            task = self._task_or_raise(uow, correlation.task_id, for_update=True)
            run = uow.runs.get(correlation.run_id, for_update=True)
            if run is None or run.task_id != task.id:
                raise A2ADelegationConflict("A2A correlation Run is unavailable")
            late = task.status in TERMINAL_TASK_STATUSES or run.status in TERMINAL_RUN_STATUSES
            if direct_message is not None:
                output = self._candidate_output(correlation, direct_message, None)
                updated = correlation.terminal(
                    status=RemoteCorrelationStatus.COMPLETED,
                    remote_task_id=None,
                    remote_context_id=_optional_string(direct_message.get("contextId")),
                    remote_state="DIRECT_MESSAGE",
                    response_digest=response_digest,
                    result=output,
                    error=None,
                    late_result=late,
                    from_poll=False,
                )
                if not late:
                    task.complete_remote(run.id, output)
                    run.succeed(output)
            else:
                if not isinstance(remote_task, dict):
                    raise InvalidA2ADelegation("A2A Task response must be an object")
                updated = self._apply_remote_task(
                    correlation,
                    task,
                    run,
                    remote_task,
                    response_digest=response_digest,
                    late=late,
                    from_poll=from_poll,
                )
            uow.remote_correlations.save(updated)
            uow.tasks.save(task)
            uow.runs.save(run)
            self._event(uow, updated, "agentmesh.a2a.remote-update-normalized")
            uow.commit()
            return updated

    def _apply_remote_task(
        self,
        correlation: RemoteTaskCorrelation,
        task: Task,
        run: TaskRun,
        remote_task: dict[str, Any],
        *,
        response_digest: str,
        late: bool,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        remote_task_id = _required_string(remote_task.get("id"), "Remote Task id")
        remote_context_id = _optional_string(remote_task.get("contextId"))
        status = remote_task.get("status")
        if not isinstance(status, dict):
            raise InvalidA2ADelegation("Remote Task status must be an object")
        remote_state = _required_string(status.get("state"), "Remote Task state", max_length=128)
        if remote_state in REMOTE_ACTIVE_STATES:
            if not late:
                task.note_remote_active(run.id)
                run.note_remote_active()
            return correlation.wait_remote(
                remote_task_id=remote_task_id,
                remote_context_id=remote_context_id,
                remote_state=remote_state,
                response_digest=response_digest,
                from_poll=from_poll,
            )
        if remote_state in REMOTE_INTERVENTION_STATES or remote_state not in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_REJECTED",
            "TASK_STATE_CANCELED",
        }:
            reason = f"remote_intervention:{remote_state}"
            updated = correlation.intervention(
                remote_task_id=remote_task_id,
                remote_context_id=remote_context_id,
                remote_state=remote_state,
                response_digest=response_digest,
                error=reason,
                from_poll=from_poll,
            )
            if not late:
                task.note_remote_intervention(run.id, reason)
                run.note_remote_intervention(reason)
            return updated
        if remote_state == "TASK_STATE_COMPLETED":
            output = self._candidate_output(correlation, status.get("message"), remote_task)
            target_status = RemoteCorrelationStatus.COMPLETED
            error = None
            if not late:
                task.complete_remote(run.id, output)
                run.succeed(output)
        elif remote_state == "TASK_STATE_CANCELED":
            output = None
            target_status = RemoteCorrelationStatus.CANCELED
            error = "remote_task_canceled"
            if not late:
                task.cancel()
                run.cancel()
        else:
            output = None
            target_status = (
                RemoteCorrelationStatus.REJECTED
                if remote_state == "TASK_STATE_REJECTED"
                else RemoteCorrelationStatus.FAILED
            )
            error = (
                "remote_task_rejected"
                if target_status is RemoteCorrelationStatus.REJECTED
                else "remote_task_failed"
            )
            if not late:
                task.fail_remote(run.id, error)
                run.fail(error)
        return correlation.terminal(
            status=target_status,
            remote_task_id=remote_task_id,
            remote_context_id=remote_context_id,
            remote_state=remote_state,
            response_digest=response_digest,
            result=output,
            error=error,
            late_result=late,
            from_poll=from_poll,
        )

    def _candidate_output(
        self,
        correlation: RemoteTaskCorrelation,
        message: Any,
        remote_task: dict[str, Any] | None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if message is not None:
            if not isinstance(message, dict):
                raise InvalidA2ADelegation("A2A Message must be an object")
            parts.extend(_inline_parts(message.get("parts", [])))
        if remote_task is not None:
            artifacts = remote_task.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise InvalidA2ADelegation("Remote Task artifacts must be an array")
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise InvalidA2ADelegation("A2A Artifact must be an object")
                parts.extend(_inline_parts(artifact.get("parts", [])))
        output = {
            "source": "a2a",
            "peer_id": str(correlation.peer_id),
            "card_snapshot_id": str(correlation.card_snapshot_id),
            "remote_task_id": remote_task.get("id") if remote_task else None,
            "parts": parts,
        }
        if len(_encoded(output)) > self._max_inline_result_bytes:
            raise InvalidA2ADelegation("A2A inline result exceeds the configured size limit")
        return output

    def _transport_failure(
        self, correlation_id: UUID, failure: A2ATransportFailure
    ) -> RemoteTaskCorrelation:
        with self._uow_factory() as uow:
            correlation = self._correlation_or_raise(uow, correlation_id, for_update=True)
            task = self._task_or_raise(uow, correlation.task_id, for_update=True)
            run = uow.runs.get(correlation.run_id, for_update=True)
            if run is None:
                raise A2ADelegationConflict("A2A correlation Run is unavailable")
            if failure.request_may_have_been_sent:
                updated = correlation.outcome_unknown(error=str(failure))
                task.note_remote_intervention(run.id, "remote_send_outcome_unknown")
                run.note_remote_intervention("remote_send_outcome_unknown")
            else:
                updated = correlation.fail_before_send(error=str(failure))
                task.fail_remote(run.id, "remote_send_failed_before_delivery")
                run.fail("remote_send_failed_before_delivery")
            uow.remote_correlations.save(updated)
            uow.tasks.save(task)
            uow.runs.save(run)
            self._event(uow, updated, "agentmesh.a2a.delegation-transport-failed")
            uow.commit()
            return updated

    def _invalid_response(
        self,
        correlation_id: UUID,
        response: dict[str, Any],
        error: str,
        *,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        with self._uow_factory() as uow:
            correlation = self._correlation_or_raise(uow, correlation_id, for_update=True)
            task = self._task_or_raise(uow, correlation.task_id, for_update=True)
            run = uow.runs.get(correlation.run_id, for_update=True)
            if run is None:
                raise A2ADelegationConflict("A2A correlation Run is unavailable")
            updated = correlation.intervention(
                remote_task_id=correlation.remote_task_id,
                remote_context_id=correlation.remote_context_id,
                remote_state="INVALID_REMOTE_RESPONSE",
                response_digest=_digest(response),
                error=f"invalid_remote_response:{error}"[:2000],
                from_poll=from_poll,
            )
            if (
                task.status not in TERMINAL_TASK_STATUSES
                and run.status not in TERMINAL_RUN_STATUSES
            ):
                task.note_remote_intervention(run.id, "invalid_remote_response")
                run.note_remote_intervention("invalid_remote_response")
            uow.remote_correlations.save(updated)
            uow.tasks.save(task)
            uow.runs.save(run)
            self._event(uow, updated, "agentmesh.a2a.remote-response-rejected")
            uow.commit()
            return updated

    def _resolve_target(
        self, uow, peer_id: UUID
    ) -> tuple[A2APeer, AgentCardSnapshot, A2AEndpoint, A2ABearerRequirement | None]:
        peer = uow.a2a_registry.get_peer(peer_id)
        if (
            peer is None
            or peer.tenant_id != self._tenant_id
            or peer.status is not A2APeerStatus.ACTIVE
            or peer.active_card_snapshot_id is None
        ):
            raise InvalidA2ADelegation("A2A Peer is not active for this tenant")
        snapshot = uow.a2a_registry.get_snapshot(peer.active_card_snapshot_id)
        if snapshot is None or snapshot.tenant_id != self._tenant_id:
            raise InvalidA2ADelegation("A2A Peer active Card snapshot is unavailable")
        from agentmesh.domain.tasks import utc_now

        if snapshot.expires_at <= utc_now():
            raise InvalidA2ADelegation("A2A Peer active Card snapshot has expired")
        endpoint = next(
            (
                item
                for item in snapshot.endpoints
                if item.protocol_binding == "HTTP+JSON" and item.protocol_version == "1.0"
            ),
            None,
        )
        if endpoint is None:
            raise InvalidA2ADelegation("Peer has no supported A2A 1.0 HTTP+JSON interface")
        raw_requirements = snapshot.raw_card.get(
            "securityRequirements", snapshot.raw_card.get("security", [])
        )
        requirement = None
        if raw_requirements:
            try:
                scheme_name, scopes = bearer_requirement(snapshot)
            except InvalidCredential as exc:
                raise InvalidA2ADelegation(str(exc)) from exc
            requirement = A2ABearerRequirement(
                scheme_name=scheme_name,
                scopes=scopes,
                audience=endpoint.url.rstrip("/"),
                card_snapshot_id=snapshot.id,
                card_digest=snapshot.digest,
            )
        return peer, snapshot, endpoint, requirement

    def _policy_arguments(
        self,
        task: Task,
        peer: A2APeer,
        snapshot: AgentCardSnapshot,
        endpoint: A2AEndpoint,
        *,
        requirement: A2ABearerRequirement | None,
        credential_binding_id: UUID | None,
    ) -> dict[str, Any]:
        task_digest = _digest(
            {"objective": task.objective, "input": task.input, "task_id": str(task.id)}
        )
        message_id = uuid5(
            NAMESPACE_URL,
            f"agentmesh:a2a:{self._tenant_id}:{task.id}:{snapshot.id}:{task_digest}",
        )
        if requirement is None:
            if credential_binding_id is not None:
                raise InvalidA2ADelegation(
                    "Unauthenticated A2A Peer cannot use a CredentialBinding"
                )
        else:
            if (
                credential_binding_id is None
                or self._credential_broker is None
                or self._workload_principal_id is None
            ):
                raise InvalidA2ADelegation(
                    "Authenticated A2A Peer requires an enabled Credential Broker and Binding"
                )
            self._credential_broker.describe_a2a_binding(
                credential_binding_id,
                workload_principal_id=self._workload_principal_id,
                peer_id=peer.id,
                card_snapshot_id=snapshot.id,
                card_digest=snapshot.digest,
                audience=requirement.audience,
                scheme_name=requirement.scheme_name,
                scopes=requirement.scopes,
            )
        return {
            "task_digest": task_digest,
            "peer_id": str(peer.id),
            "card_snapshot_id": str(snapshot.id),
            "card_digest": snapshot.digest,
            "endpoint_url": endpoint.url,
            "protocol_binding": endpoint.protocol_binding,
            "protocol_version": endpoint.protocol_version,
            "endpoint_tenant": endpoint.tenant,
            "outbound_message_id": str(message_id),
            "credential_binding_id": (
                str(credential_binding_id) if credential_binding_id else None
            ),
            "credential_scheme_name": requirement.scheme_name if requirement else None,
            "credential_scopes": list(requirement.scopes) if requirement else [],
        }

    def _acquire_credential(self, correlation: RemoteTaskCorrelation):
        if correlation.credential_binding_id is None:
            return None
        if self._credential_broker is None or self._workload_principal_id is None:
            raise InvalidA2ADelegation("A2A Credential Broker is unavailable")
        grant = self._credential_broker.acquire_for_a2a(
            correlation.credential_binding_id,
            workload_principal_id=self._workload_principal_id,
            peer_id=correlation.peer_id,
            card_snapshot_id=correlation.card_snapshot_id,
            card_digest=correlation.card_digest,
            audience=correlation.endpoint_url.rstrip("/"),
            scheme_name=correlation.credential_scheme_name or "",
            scopes=correlation.credential_scopes,
            task_id=correlation.task_id,
            run_id=correlation.run_id,
        )
        with self._uow_factory() as uow:
            current = self._correlation_or_raise(uow, correlation.id, for_update=True)
            updated = current.attach_credential_lease(grant.lease.id)
            uow.remote_correlations.save(updated)
            self._event(uow, updated, "agentmesh.a2a.credential-lease-attached")
            uow.commit()
        return grant

    @staticmethod
    def _message(
        task: Task,
        correlation: RemoteTaskCorrelation,
        snapshot: AgentCardSnapshot,
    ) -> dict[str, Any]:
        input_modes = set(snapshot.raw_card.get("defaultInputModes", []))
        payload = {"objective": task.objective, "input": task.input}
        if "application/json" in input_modes:
            part = {"data": payload, "mediaType": "application/json"}
        elif "text/plain" in input_modes:
            part = {
                "text": json.dumps(payload, sort_keys=True, ensure_ascii=False),
                "mediaType": "text/plain",
            }
        else:
            raise InvalidA2ADelegation("Peer has no compatible input mode")
        message: dict[str, Any] = {
            "messageId": str(correlation.outbound_message_id),
            "role": "ROLE_USER",
            "parts": [part],
        }
        if correlation.endpoint_tenant:
            message["tenant"] = correlation.endpoint_tenant
        return message

    def _task_or_raise(self, uow, task_id: UUID, *, for_update: bool = False) -> Task:
        task = uow.tasks.get(task_id, for_update=for_update)
        if task is None or task.tenant_id != self._tenant_id:
            raise A2ADelegationNotFound(f"Task {task_id} was not found")
        return task

    def _correlation_or_raise(
        self, uow, correlation_id: UUID, *, for_update: bool = False
    ) -> RemoteTaskCorrelation:
        correlation = uow.remote_correlations.get(correlation_id, for_update=for_update)
        if correlation is None or correlation.tenant_id != self._tenant_id:
            raise A2ADelegationNotFound(f"A2A correlation {correlation_id} was not found")
        return correlation

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise InvalidA2ADelegation("A2A delegation requires an authenticated tenant Principal")

    @staticmethod
    def _replay(uow, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        normalized_key = key.strip()
        if not normalized_key:
            raise IdempotencyConflict("Idempotency-Key must not be empty")
        uow.idempotency.lock(scope, normalized_key)
        existing = uow.idempotency.get(scope, normalized_key)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different request")
        return existing.result

    @staticmethod
    def _record(uow, scope: str, key: str, request_hash: str, result: dict[str, Any]) -> None:
        uow.idempotency.add(
            IdempotencyRecord.create(
                scope=scope,
                key=key.strip(),
                request_hash=request_hash,
                result=result,
            )
        )

    def _event(self, uow, correlation: RemoteTaskCorrelation, schema_name: str) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema_name,
                tenant_id=self._tenant_id,
                aggregate_id=correlation.id,
                payload={
                    "correlation_id": str(correlation.id),
                    "task_id": str(correlation.task_id),
                    "run_id": str(correlation.run_id),
                    "peer_id": str(correlation.peer_id),
                    "card_snapshot_id": str(correlation.card_snapshot_id),
                    "status": correlation.status.value,
                    "remote_task_id": correlation.remote_task_id,
                    "response_digest": correlation.last_response_digest,
                },
            )
        )


def _inline_parts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidA2ADelegation("A2A parts must be an array")
    normalized: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            raise InvalidA2ADelegation("A2A Part must be an object")
        content_keys = [key for key in ("text", "data", "raw", "url") if key in part]
        if len(content_keys) != 1 or content_keys[0] not in {"text", "data"}:
            raise InvalidA2ADelegation(
                "This baseline accepts only single-content inline text/data A2A Parts"
            )
        key = content_keys[0]
        if key == "text" and not isinstance(part[key], str):
            raise InvalidA2ADelegation("A2A text Part must contain a string")
        normalized_part = {key: part[key]}
        if isinstance(part.get("mediaType"), str):
            normalized_part["media_type"] = part["mediaType"]
        normalized.append(normalized_part)
    return normalized


def _required_string(value: Any, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise InvalidA2ADelegation(f"{field} must be a non-empty bounded string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, "Remote context ID")


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_encoded(value)).hexdigest()}"
