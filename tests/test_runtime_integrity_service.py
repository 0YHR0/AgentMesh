from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_integrity_services import RuntimeIntegrityService
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentActionType,
    RuntimeIntegrityIncidentStatus,
)

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _principal(tenant: str = "tenant-a", authenticated: bool = True) -> PrincipalContext:
    return PrincipalContext(
        principal_id="operator-1",
        tenant_id=tenant,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=authenticated,
        authentication_method="test",
    )


def _incident(tenant: str = "tenant-a") -> RuntimeIntegrityIncident:
    return RuntimeIntegrityIncident(
        id=uuid4(),
        tenant_id=tenant,
        runtime_execution_id=uuid4(),
        accepted_observation_id="accepted",
        accepted_observation_digest="a" * 64,
        accepted_phase=RuntimeExecutionPhase.SUCCEEDED,
        conflicting_observation_id="conflicting",
        conflicting_observation_digest="b" * 64,
        conflicting_phase=RuntimeExecutionPhase.FAILED,
        status=RuntimeIntegrityIncidentStatus.OPEN,
        reason="late terminal conflict",
        created_at=NOW,
        updated_at=NOW,
    )


class _Idempotency:
    def __init__(self) -> None:
        self.records = {}

    def lock(self, scope: str, key: str) -> None:
        return None

    def get(self, scope: str, key: str):
        return self.records.get((scope, key))

    def add(self, record) -> None:
        self.records[(record.scope, record.key)] = record


class _Outbox:
    def __init__(self) -> None:
        self.values = []

    def add(self, value) -> None:
        self.values.append(value)


class _RuntimeRepository:
    def __init__(self, incident: RuntimeIntegrityIncident) -> None:
        self.incident = incident
        self.actions = {}

    def get_integrity_incident(self, incident_id, *, tenant_id):
        if self.incident.id != incident_id or self.incident.tenant_id != tenant_id:
            return None
        return self.incident

    def list_integrity_incidents(self, execution_id=None, *, tenant_id, status=None, limit, offset):
        values = [self.incident] if self.incident.tenant_id == tenant_id else []
        if execution_id is not None:
            values = [value for value in values if value.runtime_execution_id == execution_id]
        if status is not None:
            values = [value for value in values if value.status is status]
        return values[offset : offset + limit]

    def transition_integrity_incident(
        self, incident_id, *, tenant_id, expected_status, target_status, now
    ):
        if self.incident.status is not expected_status:
            raise RuntimeExecutionConflict("Runtime integrity incident transition lost")
        self.incident = self.incident.transition(target_status, now=now)
        return self.incident

    def add_integrity_incident_action(self, value):
        self.actions[value.id] = value
        return value

    def get_integrity_incident_action(self, action_id, *, tenant_id):
        value = self.actions.get(action_id)
        return value if value is not None and value.tenant_id == tenant_id else None

    def list_integrity_incident_actions(self, incident_id, *, tenant_id, limit, offset):
        values = [
            value
            for value in self.actions.values()
            if value.incident_id == incident_id and value.tenant_id == tenant_id
        ]
        return values[offset : offset + limit]


class _Uow:
    def __init__(self, runtime, idem, outbox):
        self.runtimes = runtime
        self.idempotency = idem
        self.outbox = outbox
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def commit(self):
        self.commits += 1


def _service(incident=None):
    runtime = _RuntimeRepository(incident or _incident())
    idem = _Idempotency()
    outbox = _Outbox()
    uow = _Uow(runtime, idem, outbox)
    return RuntimeIntegrityService(uow_factory=lambda: uow, tenant_id="tenant-a"), uow


def test_transition_is_monotonic_and_exactly_idempotent():
    service, uow = _service()
    first = service.acknowledge(
        uow.runtimes.incident.id,
        principal=_principal(),
        reason="reviewed evidence",
        idempotency_key="key-1",
        now=NOW,
    )
    replay = service.acknowledge(
        uow.runtimes.incident.id,
        principal=_principal(),
        reason="reviewed evidence",
        idempotency_key="key-1",
        now=NOW,
    )
    assert replay == first
    assert len(uow.runtimes.actions) == 1
    assert len(uow.outbox.values) == 1
    assert first.action.action is RuntimeIntegrityIncidentActionType.ACKNOWLEDGE
    with pytest.raises(InvalidTaskTransition):
        service.acknowledge(
            uow.runtimes.incident.id,
            principal=_principal(),
            reason="duplicate state, new request",
            idempotency_key="key-2",
            now=NOW,
        )


def test_incident_transition_rejects_clock_regression_and_invalid_action_shape():
    incident = _incident()
    with pytest.raises(InvalidTaskTransition):
        incident.transition(
            RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
            now=NOW.replace(year=2025),
        )
    with pytest.raises(InvalidTaskInput):
        RuntimeIntegrityIncidentAction(
            id=uuid4(),
            tenant_id=incident.tenant_id,
            incident_id=incident.id,
            action=RuntimeIntegrityIncidentActionType.ACKNOWLEDGE,
            from_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
            to_status=RuntimeIntegrityIncidentStatus.ESCALATED,
            actor_principal_id="operator-1",
            reason="invalid shape",
            request_digest="c" * 64,
            created_at=NOW,
        )


def test_changed_request_with_same_key_is_rejected():
    service, uow = _service()
    service.acknowledge(
        uow.runtimes.incident.id,
        principal=_principal(),
        reason="one",
        idempotency_key="key-1",
        now=NOW,
    )
    with pytest.raises(IdempotencyConflict):
        service.acknowledge(
            uow.runtimes.incident.id,
            principal=_principal(),
            reason="changed",
            idempotency_key="key-1",
            now=NOW,
        )


def test_replay_returns_original_ack_snapshot_after_later_escalation():
    service, uow = _service()
    incident_id = uow.runtimes.incident.id
    acknowledged = service.acknowledge(
        incident_id,
        principal=_principal(),
        reason="ack reason",
        idempotency_key="ack-key",
        now=NOW,
    )
    escalated = service.escalate(
        incident_id,
        principal=_principal(),
        reason="escalate reason",
        idempotency_key="escalate-key",
        now=NOW,
    )
    replay = service.acknowledge(
        incident_id,
        principal=_principal(),
        reason="ack reason",
        idempotency_key="ack-key",
        now=NOW,
    )
    assert escalated.incident.status is RuntimeIntegrityIncidentStatus.ESCALATED
    assert replay == acknowledged
    assert replay.incident.status is RuntimeIntegrityIncidentStatus.ACKNOWLEDGED
    assert len(uow.runtimes.actions) == 2
    assert len(uow.outbox.values) == 2


def test_corrupt_idempotency_snapshot_fails_closed():
    service, uow = _service()
    incident_id = uow.runtimes.incident.id
    service.acknowledge(
        incident_id,
        principal=_principal(),
        reason="ack reason",
        idempotency_key="ack-key",
        now=NOW,
    )
    record = uow.idempotency.records[
        (f"runtime-integrity-incident:tenant-a:{incident_id}", "ack-key")
    ]
    record.result["action"]["request_digest"] = "f" * 64
    with pytest.raises(IdempotencyConflict):
        service.acknowledge(
            incident_id,
            principal=_principal(),
            reason="ack reason",
            idempotency_key="ack-key",
            now=NOW,
        )


@pytest.mark.parametrize("mutation", ["status", "timestamp"])
def test_replay_rejects_incident_regression(mutation):
    incident = _incident()
    if mutation == "timestamp":
        incident = replace(
            incident,
            created_at=NOW - timedelta(seconds=10),
            updated_at=NOW - timedelta(seconds=5),
        )
    service, uow = _service(incident)
    incident_id = uow.runtimes.incident.id
    service.acknowledge(
        incident_id,
        principal=_principal(),
        reason="ack reason",
        idempotency_key="ack-key",
        now=NOW,
    )
    if mutation == "status":
        uow.runtimes.incident = replace(
            uow.runtimes.incident, status=RuntimeIntegrityIncidentStatus.OPEN
        )
    else:
        uow.runtimes.incident = replace(
            uow.runtimes.incident, updated_at=NOW - timedelta(seconds=1)
        )
    with pytest.raises(IdempotencyConflict):
        service.acknowledge(
            incident_id,
            principal=_principal(),
            reason="ack reason",
            idempotency_key="ack-key",
            now=NOW,
        )


@pytest.mark.parametrize("principal", [_principal("other"), _principal(authenticated=False)])
def test_operator_commands_are_authenticated_and_tenant_scoped(principal):
    service, uow = _service()
    with pytest.raises(AuthorizationDenied):
        service.escalate(
            uow.runtimes.incident.id,
            principal=principal,
            reason="not allowed",
            idempotency_key="key-1",
            now=NOW,
        )
    assert not uow.runtimes.actions
    assert not uow.outbox.values
