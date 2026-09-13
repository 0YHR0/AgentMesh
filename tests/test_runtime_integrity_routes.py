from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.api.security import get_principal_context
from agentmesh.domain.errors import AuthorizationDenied
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentActionType,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.features import FeatureGateSet


def _principal(
    tenant_id: str,
    roles=frozenset({Role.OPERATOR}),
    *,
    authenticated: bool = True,
) -> PrincipalContext:
    return PrincipalContext(
        principal_id="operator-1",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=roles,
        authenticated=authenticated,
        authentication_method="test",
    )


class _IntegrityRouteService:
    tenant_id = "test-tenant"

    def __init__(self) -> None:
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.incident = RuntimeIntegrityIncident(
            id=uuid4(),
            tenant_id=self.tenant_id,
            runtime_execution_id=uuid4(),
            accepted_observation_id="accepted",
            accepted_observation_digest="a" * 64,
            accepted_phase=RuntimeExecutionPhase.SUCCEEDED,
            conflicting_observation_id="conflicting",
            conflicting_observation_digest="b" * 64,
            conflicting_phase=RuntimeExecutionPhase.FAILED,
            status=RuntimeIntegrityIncidentStatus.OPEN,
            reason="safe incident projection",
            created_at=now,
            updated_at=now,
        )
        self.action = RuntimeIntegrityIncidentAction(
            id=uuid4(),
            tenant_id=self.tenant_id,
            incident_id=self.incident.id,
            action=RuntimeIntegrityIncidentActionType.ACKNOWLEDGE,
            from_status=RuntimeIntegrityIncidentStatus.OPEN,
            to_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
            actor_principal_id="operator-1",
            reason="reviewed",
            request_digest="c" * 64,
            created_at=now,
        )

    def _check(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self.tenant_id:
            raise AuthorizationDenied("Runtime integrity tenant scope denied")

    def list_incidents(self, *, principal, **kwargs):
        self._check(principal)
        return [self.incident]

    def get_incident(self, incident_id, *, principal):
        self._check(principal)
        return self.incident

    def list_actions(self, incident_id, *, principal, **kwargs):
        self._check(principal)
        return [self.action]

    def acknowledge(self, incident_id, *, principal, **kwargs):
        self._check(principal)
        return type("Result", (), {"incident": self.incident, "action": self.action})()

    def escalate(self, incident_id, *, principal, **kwargs):
        self._check(principal)
        return type("Result", (), {"incident": self.incident, "action": self.action})()


def test_integrity_routes_require_feature_permission_and_return_safe_projection(
    application_container,
) -> None:
    service = _IntegrityRouteService()
    application_container.runtime_integrity_service = service
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true,identity_rbac=true"
    )
    application = create_app(application_container)
    application.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id
    )
    with TestClient(application) as client:
        listed = client.get("/api/v1/runtime-integrity-incidents")
        detail = client.get(f"/api/v1/runtime-integrity-incidents/{service.incident.id}")
        actions = client.get(
            f"/api/v1/runtime-integrity-incidents/{service.incident.id}/actions"
        )
        acknowledged = client.post(
            f"/api/v1/runtime-integrity-incidents/{service.incident.id}/acknowledge",
            json={"reason": "reviewed"},
            headers={"Idempotency-Key": "route-key"},
        )
    assert listed.status_code == detail.status_code == actions.status_code == 200
    assert acknowledged.status_code == 200
    assert set(listed.json()[0]) == {
        "id",
        "runtime_execution_id",
        "accepted_observation_id",
        "accepted_observation_digest",
        "accepted_phase",
        "conflicting_observation_id",
        "conflicting_observation_digest",
        "conflicting_phase",
        "status",
        "reason",
        "created_at",
        "updated_at",
    }
    assert set(actions.json()[0]) == {
        "id",
        "incident_id",
        "action",
        "from_status",
        "to_status",
        "actor_principal_id",
        "reason",
        "request_digest",
        "created_at",
    }
    assert listed.json()[0]["status"] == "OPEN"


def test_integrity_routes_reject_feature_off_wrong_tenant_and_missing_permission(
    application_container,
) -> None:
    service = _IntegrityRouteService()
    application_container.runtime_integrity_service = service
    application_container.feature_gates = FeatureGateSet.from_config("minimal")
    feature_off = create_app(application_container)
    feature_off.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id
    )
    with TestClient(feature_off) as client:
        assert client.get("/api/v1/runtime-integrity-incidents").status_code == 403

    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true,identity_rbac=true"
    )
    wrong_tenant = create_app(application_container)
    wrong_tenant.dependency_overrides[get_principal_context] = lambda: _principal("other")
    with TestClient(wrong_tenant) as client:
        assert client.get("/api/v1/runtime-integrity-incidents").status_code == 403

    anonymous = create_app(application_container)
    anonymous.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id, authenticated=False
    )
    with TestClient(anonymous) as client:
        assert client.get("/api/v1/runtime-integrity-incidents").status_code == 403
        for operation in ("acknowledge", "escalate"):
            assert (
                client.post(
                    f"/api/v1/runtime-integrity-incidents/{service.incident.id}/{operation}",
                    json={"reason": "reviewed"},
                    headers={"Idempotency-Key": f"anonymous-{operation}"},
                ).status_code
                == 403
            )

    no_permission = create_app(application_container)
    no_permission.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id, frozenset({Role.AGENT_AUTHOR})
    )
    with TestClient(no_permission) as client:
        assert client.get("/api/v1/runtime-integrity-incidents").status_code == 403
        assert (
            client.post(
                f"/api/v1/runtime-integrity-incidents/{service.incident.id}/acknowledge",
                json={"reason": "reviewed"},
                headers={"Idempotency-Key": "denied"},
            ).status_code
            == 403
        )

    auditor = create_app(application_container)
    auditor.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id, frozenset({Role.AUDITOR})
    )
    with TestClient(auditor) as client:
        assert client.get("/api/v1/runtime-integrity-incidents").status_code == 200
        for operation in ("acknowledge", "escalate"):
            assert (
                client.post(
                    f"/api/v1/runtime-integrity-incidents/{service.incident.id}/{operation}",
                    json={"reason": "reviewed"},
                    headers={"Idempotency-Key": f"auditor-{operation}"},
                ).status_code
                == 403
            )
