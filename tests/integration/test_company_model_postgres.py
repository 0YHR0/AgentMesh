import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.config import get_settings
from agentmesh.domain.company import AppointmentStatus, OrganizationNodeType
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_company_model_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"company-integration-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    registry = AgentRegistryService(uow_factory=factory, tenant_id=tenant_id)
    service = CompanyModelService(
        uow_factory=factory,
        tenant_id=tenant_id,
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )
    try:
        agent = registry.ensure_builtin_agent(f"employee-{uuid4().hex[:8]}")
        company = service.create_company(
            name="PostgreSQL Company",
            mission="Verify durable organization state.",
            owner_principal_id="integration-owner",
        )
        unit = service.create_unit(
            company.id,
            key="delivery",
            name="Delivery",
            kind="studio",
            purpose="Deliver verified digital work.",
        )
        position = service.create_position(
            company.id,
            primary_unit_id=unit.id,
            key="specialist",
            title="Specialist",
            responsibility_contract={"outcomes": ["accepted work"]},
            required_capabilities=["general.task"],
        )
        relationship = service.create_relationship(
            company.id,
            relationship_type="member.of",
            source_type=OrganizationNodeType.POSITION,
            source_id=position.id,
            target_type=OrganizationNodeType.UNIT,
            target_id=unit.id,
        )
        appointment = service.appoint(
            company.id,
            position_id=position.id,
            agent_definition_id=agent.definition.id,
            agent_version_id=agent.definition.default_version_id,
            appointed_by="integration-owner",
            reason="Published and qualified Agent Version.",
        )
        ended = service.end_appointment(company.id, appointment.id)
        snapshot = service.get_company(company.id)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT c.name, u.kind, p.key, a.status "
                    "FROM companies c "
                    "JOIN organization_units u ON u.company_id = c.id "
                    "JOIN company_positions p ON p.primary_unit_id = u.id "
                    "JOIN company_appointments a ON a.position_id = p.id "
                    "WHERE c.id = :company_id"
                ),
                {"company_id": company.id},
            ).one()

        assert ended.status is AppointmentStatus.ENDED
        assert snapshot.relationships[0].id == relationship.id
        assert row == ("PostgreSQL Company", "studio", "specialist", "ENDED")
    finally:
        engine.dispose()
