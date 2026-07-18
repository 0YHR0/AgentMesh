import json
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.api.security import get_principal_context
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.bootstrap import ApplicationContainer, build_api_container
from agentmesh.config import Settings
from agentmesh.domain.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    AuthorizationDenied,
    InvalidIdentityConfiguration,
)
from agentmesh.domain.identity import Permission, Role
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    TaskExecutionMode,
)
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory
from tests.test_task_resolutions import _process_latest

TOKENS = {
    "admin": "admin-token-00000000000000000000000000000000",
    "operator": "operator-token-00000000000000000000000000000",
    "author": "author-token-000000000000000000000000000000",
    "publisher": "publisher-token-0000000000000000000000000000",
    "auditor": "auditor-token-00000000000000000000000000000",
}


def _principal(
    principal_id: str,
    role: Role,
    *,
    tenant_id: str = "test-tenant",
    token: str | None = None,
    status: str = "ACTIVE",
    expires_at: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "principal_id": principal_id,
        "tenant_id": tenant_id,
        "principal_type": "USER",
        "status": status,
        "roles": [role.value],
        "token_sha256": sha256((token or TOKENS[principal_id]).encode()).hexdigest(),
    }
    if expires_at is not None:
        value["expires_at"] = expires_at
    return value


def _identity(*principals: dict[str, object]) -> IdentityService:
    return IdentityService(
        enabled=True,
        tenant_id="test-tenant",
        principals_json=json.dumps(principals),
    )


def _secured_container(
    container: ApplicationContainer,
    *principals: dict[str, object],
) -> ApplicationContainer:
    return replace(
        container,
        feature_gates=FeatureGateSet.from_config("full", "identity_rbac=true"),
        identity_service=_identity(*principals),
    )


def _headers(principal_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[principal_id]}"}


def _dependency_calls(dependant) -> set[object]:
    calls = {dependant.call}
    for dependency in dependant.dependencies:
        calls.update(_dependency_calls(dependency))
    return calls


def test_identity_configuration_and_authentication_fail_closed() -> None:
    with pytest.raises(InvalidIdentityConfiguration, match="at least one"):
        IdentityService(enabled=True, tenant_id="test-tenant")
    with pytest.raises(InvalidIdentityConfiguration, match="valid JSON"):
        IdentityService(enabled=True, tenant_id="test-tenant", principals_json="[")
    with pytest.raises(InvalidIdentityConfiguration, match="digests must be unique"):
        _identity(
            _principal("admin", Role.TENANT_ADMIN),
            _principal("operator", Role.OPERATOR, token=TOKENS["admin"]),
        )

    identity = _identity(_principal("admin", Role.TENANT_ADMIN))
    with pytest.raises(AuthenticationRequired):
        identity.authenticate(None)
    with pytest.raises(AuthenticationFailed):
        identity.authenticate("Basic not-supported")
    with pytest.raises(AuthenticationFailed):
        identity.authenticate("Bearer too-short")
    with pytest.raises(AuthenticationFailed):
        identity.authenticate("Bearer " + "x" * 40)


def test_api_container_fails_before_opening_runtime_without_identity_configuration() -> None:
    settings = Settings(
        feature_profile="minimal",
        feature_gates="identity_rbac=true",
        identity_principals_json="[]",
    )

    with pytest.raises(InvalidIdentityConfiguration, match="at least one"):
        build_api_container(settings)


@pytest.mark.parametrize(
    "principal",
    [
        _principal("operator", Role.OPERATOR, status="SUSPENDED"),
        _principal(
            "operator",
            Role.OPERATOR,
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        ),
        _principal("operator", Role.OPERATOR, tenant_id="other-tenant"),
    ],
)
def test_inactive_expired_and_cross_tenant_principals_are_rejected(
    principal: dict[str, object],
) -> None:
    with pytest.raises(AuthenticationFailed):
        _identity(principal).authenticate(f"Bearer {TOKENS['operator']}")


def test_role_permissions_are_default_deny() -> None:
    identity = _identity(_principal("auditor", Role.AUDITOR))
    principal = identity.authenticate(f"Bearer {TOKENS['auditor']}")

    identity.authorize(principal, Permission.TASK_READ)
    with pytest.raises(AuthorizationDenied, match="task:create"):
        identity.authorize(principal, Permission.TASK_CREATE)


def test_api_authenticates_all_control_endpoints_and_separates_roles(
    application_container: ApplicationContainer,
) -> None:
    secured = _secured_container(
        application_container,
        _principal("admin", Role.TENANT_ADMIN),
        _principal("operator", Role.OPERATOR),
        _principal("author", Role.AGENT_AUTHOR),
        _principal("publisher", Role.AGENT_PUBLISHER),
        _principal("auditor", Role.AUDITOR),
    )
    with TestClient(create_app(secured)) as client:
        assert client.get("/health").status_code == 200
        missing = client.get("/api/v1/tasks")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert missing.json()["code"] == "authentication_failed"

        assert client.get("/api/v1/tasks", headers=_headers("auditor")).status_code == 200
        denied_create = client.post(
            "/api/v1/tasks",
            headers=_headers("auditor"),
            json={"objective": "Auditors cannot create Tasks"},
        )
        assert denied_create.status_code == 403
        assert denied_create.json()["code"] == "authorization_denied"

        created = client.post(
            "/api/v1/tasks",
            headers=_headers("operator"),
            json={"objective": "Operators can create Tasks"},
        )
        assert created.status_code == 201
        denied_agent = client.post(
            "/api/v1/agents",
            headers=_headers("operator"),
            json={"owner_id": "ops", "name": "operator-agent"},
        )
        assert denied_agent.status_code == 403

        authored = client.post(
            "/api/v1/agents",
            headers=_headers("author"),
            json={"owner_id": "author", "name": "authored-agent"},
        )
        assert authored.status_code == 201
        assert client.get("/api/v1/tasks", headers=_headers("author")).status_code == 403
        denied_publish = client.post(
            "/api/v1/agent-versions/00000000-0000-0000-0000-000000000000/publish",
            headers=_headers("author"),
            json={"verified_capabilities": []},
        )
        assert denied_publish.status_code == 403
        allowed_publish = client.post(
            "/api/v1/agent-versions/00000000-0000-0000-0000-000000000000/publish",
            headers=_headers("publisher"),
            json={"verified_capabilities": []},
        )
        assert allowed_publish.status_code == 404
        assert (
            client.post(
                "/api/v1/agent-candidates:search",
                headers=_headers("auditor"),
                json={"required_capabilities": []},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/features", headers=_headers("admin")).status_code == 200
        current = client.get("/api/v1/identity/me", headers=_headers("admin"))
        assert current.status_code == 200
        assert current.json() == {
            "principal_id": "admin",
            "tenant_id": "test-tenant",
            "principal_type": "USER",
            "roles": ["TENANT_ADMIN"],
            "authentication_method": "bearer_sha256",
        }


def test_every_control_api_route_has_the_principal_dependency(
    application_container: ApplicationContainer,
) -> None:
    application = create_app(application_container)
    unprotected = [
        route.path
        for route in application.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v1")
        and get_principal_context not in _dependency_calls(route.dependant)
    ]

    assert unprotected == []


def test_authenticated_principal_replaces_spoofed_resolution_actor(
    application_container: ApplicationContainer,
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    criterion = AcceptanceCriterion.create(
        key="manual",
        description="Force operator resolution",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EQUALS,
        path=["manual"],
        expected=True,
    )
    task_id = task_service.create_task(
        "Require an authenticated operator",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
        max_revisions=0,
    ).task.id
    task_service.request_run(task_id)
    _process_latest(execution_service, uow_factory)
    _process_latest(execution_service, uow_factory)

    secured = _secured_container(
        application_container,
        _principal("operator", Role.OPERATOR),
    )
    with TestClient(create_app(secured)) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/resolutions/accept-candidate",
            headers=_headers("operator"),
            json={"actor": "spoofed-admin", "reason": "Authenticated decision"},
        )

    assert response.status_code == 200
    assert response.json()["resolution"]["actor"] == "operator"
