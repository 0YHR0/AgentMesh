import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_pack_services import CompanyPackService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.config import get_settings
from agentmesh.domain.company_packs import PackKind
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_pack_resources_and_installation_commit_together_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"company-pack-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full", "company_model=true,business_objects=true,company_packs=true"
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    service = CompanyPackService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    try:
        company = company_service.create_company(
            name="Pack Integration Company",
            mission="Persist declarative Pack resources atomically.",
            owner_principal_id="owner",
        )
        pack = service.create_pack(
            key=f"integration.{uuid4().hex}",
            version="1.0.0",
            name="Integration Pack",
            kind=PackKind.DOMAIN,
            manifest={
                "resources": [
                    {
                        "kind": "organization_unit",
                        "key": "research",
                        "name": "Research",
                        "purpose": "Own verified research.",
                    },
                    {
                        "kind": "position",
                        "key": "research-lead",
                        "unit_key": "research",
                        "title": "Research Lead",
                        "responsibility_contract": {"outcome": "Verified evidence."},
                    },
                ]
            },
            required_features=["company_model"],
        )
        service.publish_pack(pack.id)
        preview = service.preview(company.id, pack.id)
        installed = service.install(
            company.id,
            pack.id,
            expected_digest=preview.content_digest,
            installed_by="owner",
        )
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM organization_units WHERE company_id=:company_id), "
                    "(SELECT count(*) FROM positions WHERE company_id=:company_id), "
                    "(SELECT count(*) FROM company_pack_installations "
                    " WHERE company_id=:company_id)"
                ),
                {"company_id": company.id},
            ).one()
        assert tuple(counts) == (1, 1, 1)
        assert len(installed.resource_refs) == 2
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM companies WHERE tenant_id=:tenant_id"),
                {"tenant_id": tenant_id},
            )
        engine.dispose()
