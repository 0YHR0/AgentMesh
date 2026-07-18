import json
import os
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.api.app import create_app
from agentmesh.application.identity_services import IdentityAdministrationService
from agentmesh.bootstrap import build_api_container
from agentmesh.config import get_settings
from agentmesh.domain.identity import PrincipalStatus, PrincipalType, Role
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_persistent_identity_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"identity-integration-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    service = IdentityAdministrationService(uow_factory=factory, tenant_id=tenant_id)
    try:
        principal = service.create_principal(
            principal_type=PrincipalType.USER,
            display_name="Integration User",
            actor="integration-admin",
            idempotency_key="create-user",
        )
        identity = service.add_external_identity(
            principal.id,
            issuer="https://idp.example",
            subject="integration-subject",
            actor="integration-admin",
            idempotency_key="map-user",
        )
        binding = service.grant_role(
            principal.id,
            role=Role.OPERATOR,
            actor="integration-admin",
            effective_at=None,
            expires_at=None,
            idempotency_key="grant-role",
        )
        revoked = service.revoke_role(
            binding.id,
            actor="integration-admin",
            reason="Integration revocation",
        )
        suspended = service.change_status(
            principal.id,
            status=PrincipalStatus.SUSPENDED,
            actor="integration-admin",
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT p.status, rb.status AS role_status, ei.subject "
                    "FROM principals p "
                    "JOIN role_bindings rb ON rb.principal_id = p.id "
                    "JOIN external_identities ei ON ei.principal_id = p.id "
                    "WHERE p.id = :principal_id"
                ),
                {"principal_id": principal.id},
            ).one()
        assert identity.principal_id == principal.id
        assert revoked.status.value == "REVOKED"
        assert suspended.status is PrincipalStatus.SUSPENDED
        assert row == ("SUSPENDED", "REVOKED", "integration-subject")
    finally:
        engine.dispose()


def test_persistent_identity_bootstrap_and_api_in_postgres() -> None:
    token = "integration-admin-token-00000000000000000000000000000"
    principal_id = uuid4()
    settings = get_settings().model_copy(
        update={
            "tenant_id": f"identity-api-{uuid4().hex}",
            "feature_profile": "minimal",
            "feature_gates": "identity_rbac=true,persistent_identity=true",
            "identity_principals_json": json.dumps(
                [
                    {
                        "principal_id": str(principal_id),
                        "tenant_id": "placeholder",
                        "principal_type": "USER",
                        "status": "ACTIVE",
                        "roles": ["TENANT_ADMIN"],
                        "token_sha256": sha256(token.encode()).hexdigest(),
                    }
                ]
            ),
        }
    )
    configured = json.loads(settings.identity_principals_json)
    configured[0]["tenant_id"] = settings.tenant_id
    settings = settings.model_copy(update={"identity_principals_json": json.dumps(configured)})
    container = build_api_container(settings)
    try:
        with TestClient(create_app(container)) as client:
            response = client.get(
                "/api/v1/identity/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        assert response.json()["principal_id"] == str(principal_id)
        assert response.json()["roles"] == ["TENANT_ADMIN"]
    finally:
        container.close()
