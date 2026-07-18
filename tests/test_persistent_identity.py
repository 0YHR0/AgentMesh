import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.identity_services import (
    IdentityAdministrationService,
    IdentityService,
)
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.errors import AuthenticationFailed, IdempotencyConflict
from agentmesh.domain.identity import PrincipalStatus, PrincipalType, Role
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.integrations.oidc import OidcJwtVerifier, VerifiedOidcIdentity
from tests.fakes import InMemoryUnitOfWorkFactory

ADMIN_ID = UUID("10000000-0000-0000-0000-000000000001")
ADMIN_TOKEN = "persistent-admin-token-000000000000000000000000000000"


def _configured_admin() -> str:
    return json.dumps(
        [
            {
                "principal_id": str(ADMIN_ID),
                "tenant_id": "test-tenant",
                "principal_type": "USER",
                "status": "ACTIVE",
                "roles": ["TENANT_ADMIN"],
                "token_sha256": sha256(ADMIN_TOKEN.encode()).hexdigest(),
            }
        ]
    )


class _FakeOidcVerifier:
    def verify(self, token: str) -> VerifiedOidcIdentity:
        assert token == "oidc-token-000000000000000000000000000000000"
        return VerifiedOidcIdentity(issuer="https://idp.example", subject="user-123")


def _persistent_services(uow_factory: InMemoryUnitOfWorkFactory):
    administration = IdentityAdministrationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    )
    identity = IdentityService(
        enabled=True,
        tenant_id="test-tenant",
        principals_json=_configured_admin(),
        persistent=True,
        uow_factory=uow_factory,
        oidc_verifier=_FakeOidcVerifier(),
    )
    administration.bootstrap(identity.configured_principals)
    return identity, administration


def test_persistent_roles_are_resolved_and_revoked_for_every_request() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    identity, administration = _persistent_services(uow_factory)
    admin = identity.authenticate(f"Bearer {ADMIN_TOKEN}")
    assert admin.roles == frozenset({Role.TENANT_ADMIN})

    user = administration.create_principal(
        principal_type=PrincipalType.USER,
        display_name="OIDC User",
        actor=admin.principal_id,
        idempotency_key="create-user",
    )
    external = administration.add_external_identity(
        user.id,
        issuer="https://idp.example/",
        subject="user-123",
        actor=admin.principal_id,
        idempotency_key="map-user",
    )
    assert external.issuer == "https://idp.example"
    binding = administration.grant_role(
        user.id,
        role=Role.OPERATOR,
        actor=admin.principal_id,
        effective_at=None,
        expires_at=None,
        idempotency_key="grant-operator",
    )

    oidc = identity.authenticate("Bearer oidc-token-000000000000000000000000000000000")
    assert oidc.principal_id == str(user.id)
    assert oidc.roles == frozenset({Role.OPERATOR})
    assert oidc.authentication_method == "oidc"

    administration.revoke_role(binding.id, actor=admin.principal_id, reason="Access removed")
    with pytest.raises(AuthenticationFailed, match="no active role"):
        identity.authenticate("Bearer oidc-token-000000000000000000000000000000000")


def test_identity_administration_is_idempotent_and_status_fails_closed() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    identity, administration = _persistent_services(uow_factory)
    actor = identity.authenticate(f"Bearer {ADMIN_TOKEN}").principal_id
    first = administration.create_principal(
        principal_type=PrincipalType.SERVICE,
        display_name="Worker",
        actor=actor,
        idempotency_key="worker",
    )
    replay = administration.create_principal(
        principal_type=PrincipalType.SERVICE,
        display_name="Worker",
        actor=actor,
        idempotency_key="worker",
    )
    assert replay.id == first.id
    with pytest.raises(IdempotencyConflict):
        administration.create_principal(
            principal_type=PrincipalType.SERVICE,
            display_name="Different",
            actor=actor,
            idempotency_key="worker",
        )

    administration.change_status(ADMIN_ID, status=PrincipalStatus.SUSPENDED, actor=actor)
    with pytest.raises(AuthenticationFailed, match="not active"):
        identity.authenticate(f"Bearer {ADMIN_TOKEN}")


def test_bootstrap_does_not_restore_a_revoked_configured_role() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    identity, administration = _persistent_services(uow_factory)
    with uow_factory() as uow:
        binding = uow.identity.list_role_bindings(ADMIN_ID)[0]
    administration.revoke_role(binding.id, actor="security-admin", reason="Compromised")
    administration.bootstrap(identity.configured_principals)

    with pytest.raises(AuthenticationFailed, match="no active role"):
        identity.authenticate(f"Bearer {ADMIN_TOKEN}")


def test_persistent_identity_admin_api(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    identity, administration = _persistent_services(uow_factory)
    container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config(
            "full", "identity_rbac=true,persistent_identity=true"
        ),
        identity_service=identity,
        identity_administration_service=administration,
    )
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}", "Idempotency-Key": "api-user"}
    with TestClient(create_app(container)) as client:
        created = client.post(
            "/api/v1/identity/principals",
            headers=headers,
            json={"principal_type": "USER", "display_name": "API User"},
        )
        assert created.status_code == 201
        principal_id = created.json()["id"]
        replay = client.post(
            "/api/v1/identity/principals",
            headers=headers,
            json={"principal_type": "USER", "display_name": "API User"},
        )
        assert replay.json()["id"] == principal_id

        granted = client.post(
            f"/api/v1/identity/principals/{principal_id}/role-bindings",
            headers={
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Idempotency-Key": "api-role",
            },
            json={"role": "AUDITOR"},
        )
        assert granted.status_code == 201
        listed = client.get(
            f"/api/v1/identity/principals/{principal_id}/role-bindings",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert [item["role"] for item in listed.json()] == ["AUDITOR"]
        assert (
            client.get(
                "/api/v1/identity/principals",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            ).status_code
            == 200
        )


class _StaticJwks:
    def __init__(self, key) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self._key)


def test_oidc_verifier_checks_signature_issuer_audience_and_time() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "https://idp.example",
        "sub": "user-123",
        "aud": "agentmesh-api",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})
    verifier = OidcJwtVerifier(
        issuer="https://idp.example",
        audience="agentmesh-api",
    )
    verifier._jwks = _StaticJwks(private_key.public_key())
    assert verifier.verify(token) == VerifiedOidcIdentity(
        issuer="https://idp.example", subject="user-123"
    )

    wrong_audience = OidcJwtVerifier(
        issuer="https://idp.example",
        audience="other-api",
    )
    wrong_audience._jwks = _StaticJwks(private_key.public_key())
    with pytest.raises(AuthenticationFailed):
        wrong_audience.verify(token)


def test_persistent_identity_feature_requires_identity() -> None:
    with pytest.raises(Exception, match="identity_rbac"):
        FeatureGateSet.from_config("minimal", "persistent_identity=true")
    enabled = FeatureGateSet.from_config("minimal", "identity_rbac=true,persistent_identity=true")
    assert enabled.is_enabled(Feature.PERSISTENT_IDENTITY)
