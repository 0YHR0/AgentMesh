"""Operator controls for safe Runtime integrity incidents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    RuntimeExecutionConflict,
    RuntimeExecutionNotFound,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentActionType,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.runtime_sdk import canonical_digest


@dataclass(frozen=True)
class RuntimeIntegrityCommandResult:
    incident: RuntimeIntegrityIncident
    action: RuntimeIntegrityIncidentAction


class RuntimeIntegrityService:
    """Tenant-scoped, idempotent operator API for incident transitions."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def list_incidents(
        self,
        *,
        principal: PrincipalContext,
        execution_id: UUID | None = None,
        status: RuntimeIntegrityIncidentStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RuntimeIntegrityIncident]:
        self._require_principal(principal)
        if not 1 <= limit <= 100 or offset < 0:
            raise InvalidTaskInput("Incident pagination is invalid")
        if status is not None and type(status) is not RuntimeIntegrityIncidentStatus:
            raise InvalidTaskInput("Incident status is invalid")
        with self._uow_factory() as uow:
            return uow.runtimes.list_integrity_incidents(
                execution_id,
                tenant_id=self._tenant_id,
                status=status,
                limit=limit,
                offset=offset,
            )

    def get_incident(
        self, incident_id: UUID, *, principal: PrincipalContext
    ) -> RuntimeIntegrityIncident:
        self._require_principal(principal)
        with self._uow_factory() as uow:
            incident = uow.runtimes.get_integrity_incident(
                incident_id, tenant_id=self._tenant_id
            )
        if incident is None:
            raise RuntimeExecutionNotFound("Runtime integrity incident was not found")
        return incident

    def list_actions(
        self,
        incident_id: UUID,
        *,
        principal: PrincipalContext,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RuntimeIntegrityIncidentAction]:
        self._require_principal(principal)
        if not 1 <= limit <= 100 or offset < 0:
            raise InvalidTaskInput("Incident action pagination is invalid")
        with self._uow_factory() as uow:
            if uow.runtimes.get_integrity_incident(incident_id, tenant_id=self._tenant_id) is None:
                raise RuntimeExecutionNotFound("Runtime integrity incident was not found")
            return uow.runtimes.list_integrity_incident_actions(
                incident_id, tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def acknowledge(
        self,
        incident_id: UUID,
        *,
        principal: PrincipalContext,
        reason: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> RuntimeIntegrityCommandResult:
        return self._transition(
            incident_id,
            action=RuntimeIntegrityIncidentActionType.ACKNOWLEDGE,
            principal=principal,
            reason=reason,
            idempotency_key=idempotency_key,
            now=now,
        )

    def escalate(
        self,
        incident_id: UUID,
        *,
        principal: PrincipalContext,
        reason: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> RuntimeIntegrityCommandResult:
        return self._transition(
            incident_id,
            action=RuntimeIntegrityIncidentActionType.ESCALATE,
            principal=principal,
            reason=reason,
            idempotency_key=idempotency_key,
            now=now,
        )

    def _transition(
        self,
        incident_id: UUID,
        *,
        action: RuntimeIntegrityIncidentActionType,
        principal: PrincipalContext,
        reason: str,
        idempotency_key: str,
        now: datetime | None,
    ) -> RuntimeIntegrityCommandResult:
        self._require_principal(principal)
        normalized_reason = self._bounded_text(reason, "Incident action reason", 4096)
        key = self._bounded_text(idempotency_key, "Idempotency-Key", 200)
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidTaskInput("Incident action timestamp is invalid")
        timestamp = timestamp.astimezone(timezone.utc)
        request_digest = canonical_digest(
            {
                "tenant_id": self._tenant_id,
                "incident_id": str(incident_id),
                "action": action.value,
                "actor_principal_id": principal.principal_id,
                "reason": normalized_reason,
            }
        )
        scope = f"runtime-integrity-incident:{self._tenant_id}:{incident_id}"
        with self._uow_factory() as uow:
            uow.idempotency.lock(scope, key)
            existing = uow.idempotency.get(scope, key)
            if existing is not None:
                if existing.request_hash != request_digest:
                    raise IdempotencyConflict("Idempotency-Key request conflicts")
                return self._replay(uow, existing)
            incident = uow.runtimes.get_integrity_incident(
                incident_id, tenant_id=self._tenant_id
            )
            if incident is None:
                raise RuntimeExecutionNotFound("Runtime integrity incident was not found")
            target = (
                RuntimeIntegrityIncidentStatus.ACKNOWLEDGED
                if action is RuntimeIntegrityIncidentActionType.ACKNOWLEDGE
                else RuntimeIntegrityIncidentStatus.ESCALATED
            )
            transitioned = incident.transition(target, now=timestamp)
            audit = RuntimeIntegrityIncidentAction(
                id=uuid4(),
                tenant_id=self._tenant_id,
                incident_id=incident.id,
                action=action,
                from_status=incident.status,
                to_status=target,
                actor_principal_id=principal.principal_id,
                reason=normalized_reason,
                request_digest=request_digest,
                created_at=timestamp,
            )
            updated = uow.runtimes.transition_integrity_incident(
                incident_id,
                tenant_id=self._tenant_id,
                expected_status=incident.status,
                target_status=target,
                now=timestamp,
            )
            # Keep the repository CAS result authoritative for the returned projection.
            if updated.status is not transitioned.status:
                raise RuntimeExecutionConflict("Runtime integrity incident transition lost")
            stored_action = uow.runtimes.add_integrity_incident_action(audit)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.integrity-incident.updated",
                    tenant_id=self._tenant_id,
                    aggregate_id=incident.id,
                    producer="agentmesh-runtime-integrity-v1",
                    payload={
                        "tenant_id": self._tenant_id,
                        "incident_id": str(incident.id),
                        "runtime_execution_id": str(incident.runtime_execution_id),
                        "action": action.value,
                        "from_status": incident.status.value,
                        "to_status": target.value,
                        "action_id": str(stored_action.id),
                    },
                )
            )
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_digest,
                    result={
                        "incident_id": str(incident.id),
                        "action_id": str(stored_action.id),
                        "incident": _incident_result_snapshot(updated),
                        "action": _action_result_snapshot(stored_action),
                    },
                )
            )
            uow.commit()
            return RuntimeIntegrityCommandResult(incident=updated, action=stored_action)

    def _replay(self, uow: Any, record: IdempotencyRecord) -> RuntimeIntegrityCommandResult:
        try:
            if type(record.result) is not dict:
                raise ValueError("result is not an object")
            incident_id = UUID(record.result["incident_id"])
            action_id = UUID(record.result["action_id"])
            incident = _incident_from_result_snapshot(record.result["incident"])
            action = _action_from_result_snapshot(record.result["action"])
        except (KeyError, TypeError, ValueError, InvalidTaskInput) as exc:
            raise IdempotencyConflict("Stored idempotency result is invalid") from exc
        if (
            incident.id != incident_id
            or action.id != action_id
            or incident.tenant_id != self._tenant_id
            or action.tenant_id != self._tenant_id
            or action.incident_id != incident.id
            or action.request_digest != record.request_hash
            or action.to_status is not incident.status
            or canonical_digest(
                {
                    "tenant_id": self._tenant_id,
                    "incident_id": str(incident.id),
                    "action": action.action.value,
                    "actor_principal_id": action.actor_principal_id,
                    "reason": action.reason,
                }
            )
            != record.request_hash
        ):
            raise IdempotencyConflict("Stored idempotency result conflicts")
        persisted_incident = uow.runtimes.get_integrity_incident(
            incident.id, tenant_id=self._tenant_id
        )
        persisted_action = uow.runtimes.get_integrity_incident_action(
            action.id, tenant_id=self._tenant_id
        )
        if persisted_incident is None or persisted_action is None:
            raise IdempotencyConflict("Stored idempotency result is incomplete")
        if (
            persisted_action != action
            or persisted_incident.tenant_id != incident.tenant_id
            or persisted_incident.runtime_execution_id != incident.runtime_execution_id
            or persisted_incident.accepted_observation_id != incident.accepted_observation_id
            or persisted_incident.accepted_observation_digest
            != incident.accepted_observation_digest
            or persisted_incident.accepted_phase is not incident.accepted_phase
            or persisted_incident.conflicting_observation_id
            != incident.conflicting_observation_id
            or persisted_incident.conflicting_observation_digest
            != incident.conflicting_observation_digest
            or persisted_incident.conflicting_phase is not incident.conflicting_phase
            or persisted_incident.reason != incident.reason
            or persisted_incident.created_at != incident.created_at
        ):
            raise IdempotencyConflict("Stored idempotency result conflicts")
        return RuntimeIntegrityCommandResult(incident=incident, action=action)

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise AuthorizationDenied("Runtime integrity tenant scope denied")
        if not principal.principal_id.strip() or len(principal.principal_id) > 128:
            raise AuthorizationDenied("Runtime integrity principal is invalid")

    @staticmethod
    def _bounded_text(value: str, label: str, limit: int) -> str:
        if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit:
            raise InvalidTaskInput(f"{label} must contain 1-{limit} UTF-8 bytes")
        return value.strip()


def _incident_result_snapshot(value: RuntimeIntegrityIncident) -> dict[str, str]:
    """Serialize only the safe incident projection into idempotency storage."""
    return {
        "id": str(value.id),
        "tenant_id": value.tenant_id,
        "runtime_execution_id": str(value.runtime_execution_id),
        "accepted_observation_id": value.accepted_observation_id,
        "accepted_observation_digest": value.accepted_observation_digest,
        "accepted_phase": value.accepted_phase.value,
        "conflicting_observation_id": value.conflicting_observation_id,
        "conflicting_observation_digest": value.conflicting_observation_digest,
        "conflicting_phase": value.conflicting_phase.value,
        "status": value.status.value,
        "reason": value.reason,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def _action_result_snapshot(value: RuntimeIntegrityIncidentAction) -> dict[str, str]:
    return {
        "id": str(value.id),
        "tenant_id": value.tenant_id,
        "incident_id": str(value.incident_id),
        "action": value.action.value,
        "from_status": value.from_status.value,
        "to_status": value.to_status.value,
        "actor_principal_id": value.actor_principal_id,
        "reason": value.reason,
        "request_digest": value.request_digest,
        "created_at": value.created_at.isoformat(),
    }


def _incident_from_result_snapshot(value: Any) -> RuntimeIntegrityIncident:
    if type(value) is not dict:
        raise InvalidTaskInput("Incident result snapshot is invalid")
    return RuntimeIntegrityIncident(
        id=UUID(value["id"]),
        tenant_id=value["tenant_id"],
        runtime_execution_id=UUID(value["runtime_execution_id"]),
        accepted_observation_id=value["accepted_observation_id"],
        accepted_observation_digest=value["accepted_observation_digest"],
        accepted_phase=RuntimeExecutionPhase(value["accepted_phase"]),
        conflicting_observation_id=value["conflicting_observation_id"],
        conflicting_observation_digest=value["conflicting_observation_digest"],
        conflicting_phase=RuntimeExecutionPhase(value["conflicting_phase"]),
        status=RuntimeIntegrityIncidentStatus(value["status"]),
        reason=value["reason"],
        created_at=datetime.fromisoformat(value["created_at"]),
        updated_at=datetime.fromisoformat(value["updated_at"]),
    )


def _action_from_result_snapshot(value: Any) -> RuntimeIntegrityIncidentAction:
    if type(value) is not dict:
        raise InvalidTaskInput("Incident action result snapshot is invalid")
    return RuntimeIntegrityIncidentAction(
        id=UUID(value["id"]),
        tenant_id=value["tenant_id"],
        incident_id=UUID(value["incident_id"]),
        action=RuntimeIntegrityIncidentActionType(value["action"]),
        from_status=RuntimeIntegrityIncidentStatus(value["from_status"]),
        to_status=RuntimeIntegrityIncidentStatus(value["to_status"]),
        actor_principal_id=value["actor_principal_id"],
        reason=value["reason"],
        request_digest=value["request_digest"],
        created_at=datetime.fromisoformat(value["created_at"]),
    )
