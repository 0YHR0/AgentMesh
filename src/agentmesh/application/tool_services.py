from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import (
    IdempotencyConflict,
    TaskNotFound,
    ToolInvocationFailed,
    ToolOutcomeUnknown,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.resolutions import McpOutcomeDecision, TaskResolution, TaskResolutionAction
from agentmesh.domain.tools import (
    ToolAuthorizationStatus,
    ToolBinding,
    ToolCallResult,
    ToolExecutionAuthorization,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSideEffect,
)


@dataclass(frozen=True)
class ToolOutcomeReconciliationResult:
    invocation: ToolInvocation
    resolution: TaskResolution


class ToolInvocationService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def start(
        self,
        *,
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolInvocation:
        invocation = ToolInvocation.start(
            tenant_id=self._tenant_id,
            task_id=task_id,
            run_id=run_id,
            binding=binding,
            arguments=arguments,
        )
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            run = uow.runs.get(run_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            if run is None or run.task_id != task.id:
                raise ToolInvocationFailed("Tool Invocation references an unavailable Run")
            if binding.side_effect is not ToolSideEffect.READ_ONLY:
                authorization = uow.tool_execution_authorizations.get_for_task(
                    task.id, for_update=True
                )
                if authorization is None:
                    raise ToolInvocationFailed("MCP write Task has no durable authorization")
                if authorization.status is ToolAuthorizationStatus.EXECUTING:
                    if authorization.invocation_id is None:
                        raise ToolInvocationFailed("MCP write authorization linkage was lost")
                    previous = uow.tool_invocations.get(
                        authorization.invocation_id, for_update=True
                    )
                    if previous is None or previous.status is not ToolInvocationStatus.RUNNING:
                        raise ToolInvocationFailed("MCP write invocation linkage was lost")
                    previous.outcome_unknown(
                        "MCP write was interrupted before its outcome was persisted"
                    )
                    uow.tool_invocations.save(previous)
                    authorization.settle(previous.status)
                    uow.tool_execution_authorizations.save(authorization)
                    uow.commit()
                    raise ToolOutcomeUnknown(
                        "Previous MCP write delivery outcome is unknown; automatic replay stopped"
                    )
                uow.tool_invocations.add(invocation)
                uow.flush()
                authorization.claim(
                    invocation_id=invocation.id,
                    binding=binding,
                    arguments=arguments,
                )
                uow.tool_execution_authorizations.save(authorization)
            else:
                uow.tool_invocations.add(invocation)
            uow.commit()
        return invocation

    def succeed(self, invocation_id: UUID, result: ToolCallResult) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.succeed(result)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def fail(self, invocation_id: UUID, error: str) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.fail(error)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def outcome_unknown(self, invocation_id: UUID, error: str) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.outcome_unknown(error)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            return uow.tool_invocations.list_for_task(task.id)

    def audit_for_task(
        self, task_id: UUID
    ) -> tuple[ToolExecutionAuthorization | None, list[ToolInvocation]]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            return (
                uow.tool_execution_authorizations.get_for_task(task.id),
                uow.tool_invocations.list_for_task(task.id),
            )

    def reconcile_outcome(
        self,
        invocation_id: UUID,
        *,
        principal: PrincipalContext,
        decision: McpOutcomeDecision,
        reason: str,
        evidence_reference: str,
        evidence_digest: str,
        idempotency_key: str,
        result_digest: str | None = None,
        result_bytes: int | None = None,
        error: str | None = None,
    ) -> ToolOutcomeReconciliationResult:
        self._require_principal(principal)
        reference = _bounded_evidence(evidence_reference, evidence_digest)
        request = {
            "invocation_id": str(invocation_id),
            "decision": decision.value,
            "reason": reason.strip(),
            "evidence_reference": reference,
            "evidence_digest": evidence_digest,
            "result_digest": result_digest,
            "result_bytes": result_bytes,
            "error": error.strip() if error else None,
        }
        if decision is McpOutcomeDecision.SUCCEEDED:
            if result_digest is None or result_bytes is None or error is not None:
                raise ToolInvocationFailed(
                    "Confirmed MCP success requires result fields and forbids an error"
                )
        elif result_digest is not None or result_bytes is not None:
            raise ToolInvocationFailed(
                "Confirmed MCP failure must not include result fields"
            )
        scope = (
            f"outcome-reconciliation:mcp:{self._tenant_id}:"
            f"{principal.principal_id}:{invocation_id}"
        )
        request_hash = _request_hash(request)
        with self._uow_factory() as uow:
            replay = _replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                invocation = self._get_owned_or_raise(uow, invocation_id, for_update=False)
                resolution = uow.task_resolutions.get(UUID(replay["resolution_id"]))
                if resolution is None:
                    raise ToolInvocationFailed("MCP reconciliation audit record was lost")
                return ToolOutcomeReconciliationResult(invocation, resolution)
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            task = uow.tasks.get(invocation.task_id, for_update=True)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(invocation.task_id)
            authorization = uow.tool_execution_authorizations.get_for_task(
                task.id, for_update=True
            )
            if (
                invocation.side_effect is ToolSideEffect.READ_ONLY
                or authorization is None
                or authorization.invocation_id != invocation.id
            ):
                raise ToolInvocationFailed(
                    "Only a linked outcome-unknown MCP write can be reconciled"
                )
            if decision is McpOutcomeDecision.SUCCEEDED:
                assert result_digest is not None and result_bytes is not None
                invocation.reconcile_succeeded(
                    result_digest=result_digest, result_bytes=result_bytes
                )
                action = TaskResolutionAction.RECONCILE_MCP_SUCCEEDED
            else:
                invocation.reconcile_failed(error or reason)
                action = TaskResolutionAction.RECONCILE_MCP_FAILED
            authorization.reconcile(invocation.status)
            resolution = TaskResolution.create(
                task_id=task.id,
                action=action,
                actor=principal.principal_id,
                reason=reason,
                previous_status=task.status,
                resulting_status=task.status,
                previous_error=task.error,
                details={
                    "target_type": "MCP_INVOCATION",
                    "target_id": str(invocation.id),
                    "authorization_id": str(authorization.id),
                    "previous_outcome": "OUTCOME_UNKNOWN",
                    "confirmed_outcome": invocation.status.value,
                    "evidence_reference": reference,
                    "evidence_digest": evidence_digest,
                    "result_digest": invocation.result_digest,
                    "result_bytes": invocation.result_bytes,
                },
            )
            uow.tool_invocations.save(invocation)
            uow.tool_execution_authorizations.save(authorization)
            uow.task_resolutions.add(resolution)
            uow.outbox.add(_reconciliation_event(task.tenant_id, task.id, resolution))
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=idempotency_key.strip(),
                    request_hash=request_hash,
                    result={"resolution_id": str(resolution.id)},
                )
            )
            uow.commit()
            return ToolOutcomeReconciliationResult(invocation, resolution)

    def _get_owned_or_raise(
        self,
        uow: Any,
        invocation_id: UUID,
        *,
        for_update: bool,
    ) -> ToolInvocation:
        invocation = uow.tool_invocations.get(invocation_id, for_update=for_update)
        if invocation is None or invocation.tenant_id != self._tenant_id:
            raise ToolInvocationFailed(f"Tool Invocation {invocation_id} was not found")
        return invocation

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise ToolInvocationFailed(
                "MCP outcome reconciliation requires an authenticated tenant Principal"
            )

    @staticmethod
    def _settle_authorization(uow: Any, invocation: ToolInvocation) -> None:
        if invocation.side_effect is ToolSideEffect.READ_ONLY:
            return
        authorization = uow.tool_execution_authorizations.get_for_task(
            invocation.task_id, for_update=True
        )
        if authorization is None or authorization.invocation_id != invocation.id:
            raise ToolInvocationFailed("MCP write authorization linkage was lost")
        assert invocation.status is not ToolInvocationStatus.RUNNING
        authorization.settle(invocation.status)
        uow.tool_execution_authorizations.save(authorization)


def _bounded_evidence(reference: str, digest: str) -> str:
    normalized = reference.strip()
    if not normalized or len(normalized) > 2048:
        raise ToolInvocationFailed("Reconciliation evidence reference must be 1-2048 characters")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ToolInvocationFailed("Reconciliation evidence digest must be SHA-256")
    try:
        int(digest.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ToolInvocationFailed("Reconciliation evidence digest must be SHA-256") from exc
    return normalized


def _request_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _replay(uow, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
    normalized = key.strip()
    if not normalized:
        raise IdempotencyConflict("Idempotency-Key must not be empty")
    uow.idempotency.lock(scope, normalized)
    existing = uow.idempotency.get(scope, normalized)
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise IdempotencyConflict("Idempotency key was reused with a different request")
    return existing.result


def _reconciliation_event(
    tenant_id: str, task_id: UUID, resolution: TaskResolution
) -> MessageEnvelope:
    return MessageEnvelope.domain_event(
        schema_name="agentmesh.external-outcome.reconciled",
        tenant_id=tenant_id,
        aggregate_id=task_id,
        causation_id=resolution.id,
        payload={
            "task_id": str(task_id),
            "resolution_id": str(resolution.id),
            "action": resolution.action.value,
            "actor": resolution.actor,
            "target_type": resolution.details["target_type"],
            "target_id": resolution.details["target_id"],
        },
    )
