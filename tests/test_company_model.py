from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_services import CompanyModelService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.company import AppointmentStatus, OrganizationNodeType
from agentmesh.domain.errors import CompanyModelConflict, FeatureDisabled, InvalidCompanyModel
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _published_agent(uow_factory: InMemoryUnitOfWorkFactory):
    definition = next(
        item
        for item in uow_factory.store.agent_definitions.values()
        if item.name == "test-agent"
    )
    version = uow_factory.store.agent_versions[definition.default_version_id]
    return definition, version


def test_company_model_is_disabled_without_explicit_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = CompanyModelService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full"),
    )

    with pytest.raises(FeatureDisabled, match="company_model"):
        service.list_companies()


def test_company_model_builds_auditable_organization_graph_and_appointments(
    company_service: CompanyModelService,
    registry_service,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company = company_service.create_company(
        name="AgentMesh Studio",
        mission="Ship evidence-backed digital work.",
        owner_principal_id="owner",
        default_currency="cny",
        operating_timezone="Asia/Shanghai",
    )
    with pytest.raises(CompanyModelConflict, match="already has an active"):
        company_service.create_company(
            name="Duplicate",
            mission="Should fail.",
            owner_principal_id="owner",
        )

    engineering = company_service.create_unit(
        company.id,
        key="engineering",
        name="Engineering",
        kind="department",
        purpose="Build and verify the product.",
    )
    delivery = company_service.create_unit(
        company.id,
        key="delivery-squad",
        name="Delivery Squad",
        kind="project",
        purpose="Deliver one bounded customer outcome.",
        parent_unit_id=engineering.id,
    )
    position = company_service.create_position(
        company.id,
        primary_unit_id=delivery.id,
        key="implementer",
        title="Implementer",
        responsibility_contract={
            "outcomes": ["tested change"],
            "prohibited_actions": ["unapproved production deployment"],
        },
        required_capabilities=["general.task"],
    )
    manager = company_service.create_position(
        company.id,
        primary_unit_id=engineering.id,
        key="engineering-lead",
        title="Engineering Lead",
        responsibility_contract={"outcomes": ["accepted delivery"]},
    )
    relation = company_service.create_relationship(
        company.id,
        relationship_type="reports.to",
        source_type=OrganizationNodeType.POSITION,
        source_id=position.id,
        target_type=OrganizationNodeType.POSITION,
        target_id=manager.id,
        attributes={"matrix": False},
    )
    with pytest.raises(CompanyModelConflict, match="already exists"):
        company_service.create_relationship(
            company.id,
            relationship_type="reports.to",
            source_type=OrganizationNodeType.POSITION,
            source_id=position.id,
            target_type=OrganizationNodeType.POSITION,
            target_id=manager.id,
        )

    definition, version = _published_agent(uow_factory)
    appointment = company_service.appoint(
        company.id,
        position_id=position.id,
        agent_definition_id=definition.id,
        agent_version_id=version.id,
        appointed_by="owner",
        reason="Qualified for the deterministic delivery role.",
    )
    with pytest.raises(CompanyModelConflict, match="already has"):
        company_service.appoint(
            company.id,
            position_id=position.id,
            agent_definition_id=definition.id,
            agent_version_id=version.id,
            appointed_by="owner",
            reason="Duplicate appointment.",
        )

    ended = company_service.end_appointment(company.id, appointment.id)
    snapshot = company_service.get_active_company()

    assert company.default_currency == "CNY"
    assert relation.relationship_type == "reports.to"
    assert ended.status is AppointmentStatus.ENDED
    assert ended.ends_at is not None
    assert [item.key for item in snapshot.units] == ["engineering", "delivery-squad"]
    assert {item.key for item in snapshot.positions} == {"implementer", "engineering-lead"}
    assert len(snapshot.relationships) == 1
    assert len(uow_factory.store.outbox) >= 8


def test_appointment_rejects_wrong_or_unqualified_agent_version(
    company_service: CompanyModelService,
) -> None:
    company = company_service.create_company(
        name="Qualification Test",
        mission="Verify appointment admission.",
        owner_principal_id="owner",
    )
    unit = company_service.create_unit(
        company.id,
        key="specialists",
        name="Specialists",
        kind="guild",
        purpose="Hold specialized roles.",
    )
    position = company_service.create_position(
        company.id,
        primary_unit_id=unit.id,
        key="security-reviewer",
        title="Security Reviewer",
        responsibility_contract={"outcomes": ["security review"]},
        required_capabilities=["security.review"],
    )

    with pytest.raises(InvalidCompanyModel, match="must belong"):
        company_service.appoint(
            company.id,
            position_id=position.id,
            agent_definition_id=uuid4(),
            agent_version_id=uuid4(),
            appointed_by="owner",
            reason="Invalid fixture.",
        )


def test_company_api_is_feature_gated_and_exposes_snapshot(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    with TestClient(create_app(application_container)) as client:
        disabled = client.get("/api/v1/companies")
        assert disabled.status_code == 403
        assert disabled.json()["code"] == "feature_disabled"

    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "company_model=true"
    )
    with TestClient(create_app(application_container)) as client:
        company_response = client.post(
            "/api/v1/companies",
            json={
                "name": "API Company",
                "mission": "Exercise the complete organization API.",
                "default_currency": "USD",
                "operating_timezone": "UTC",
            },
        )
        assert company_response.status_code == 201
        company_id = company_response.json()["id"]
        unit_response = client.post(
            f"/api/v1/companies/{company_id}/units",
            json={
                "key": "product",
                "name": "Product",
                "kind": "department",
                "purpose": "Own product outcomes.",
            },
        )
        assert unit_response.status_code == 201
        position_response = client.post(
            f"/api/v1/companies/{company_id}/positions",
            json={
                "primary_unit_id": unit_response.json()["id"],
                "key": "product-owner",
                "title": "Product Owner",
                "responsibility_contract": {"outcomes": ["accepted roadmap"]},
                "required_capabilities": ["general.task"],
            },
        )
        assert position_response.status_code == 201
        definition, version = _published_agent(uow_factory)
        appointment_response = client.post(
            f"/api/v1/companies/{company_id}/appointments",
            json={
                "position_id": position_response.json()["id"],
                "agent_definition_id": str(definition.id),
                "agent_version_id": str(version.id),
                "reason": "API qualification passed.",
            },
        )
        assert appointment_response.status_code == 201

        snapshot = client.get("/api/v1/companies/active")
        assert snapshot.status_code == 200
        body = snapshot.json()
        assert body["company"]["name"] == "API Company"
        assert body["units"][0]["kind"] == "department"
        assert body["positions"][0]["key"] == "product-owner"
        assert body["appointments"][0]["status"] == "ACTIVE"

        office_script = client.get("/console/assets/world3d.js")
        assert office_script.status_code == 200
        assert 'featureEnabled("company_model")' in office_script.text
        assert 'api("/api/v1/companies/active")' in office_script.text
        assert "employee.positionTitle" in office_script.text
        assert "employee.organizationUnitName" in office_script.text
