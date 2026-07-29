import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.business_objects import (
    BusinessObjectTypeStatus,
    ObjectSourceType,
)
from agentmesh.domain.errors import (
    BusinessObjectConflict,
    FeatureDisabled,
    InvalidBusinessObject,
)
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _company(company_service: CompanyModelService):
    company = company_service.create_company(
        name="Object Company",
        mission="Operate on typed, evidence-backed business records.",
        owner_principal_id="owner",
    )
    unit = company_service.create_unit(
        company.id,
        key="sales",
        name="Sales",
        kind="department",
        purpose="Own customer qualification.",
    )
    position = company_service.create_position(
        company.id,
        primary_unit_id=unit.id,
        key="sales-analyst",
        title="Sales Analyst",
        responsibility_contract={"outcomes": ["qualified lead"]},
        required_capabilities=["crm.qualify"],
    )
    return company, position


def _lead_type(
    service: BusinessObjectService,
    company_id,
    *,
    schema_version: int = 1,
):
    value = service.create_type(
        company_id,
        key="lead",
        name="Lead",
        schema_version=schema_version,
        json_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "email": {"type": "string", "minLength": 3},
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "notes": {"type": "string"},
            },
            "required": ["name", "email"],
            "additionalProperties": False,
        },
        lifecycle_definition={
            "states": ["NEW", "QUALIFIED", "REJECTED"],
            "initial_state": "NEW",
            "actions": {
                "qualify": {
                    "from": ["NEW"],
                    "to": "QUALIFIED",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 80},
                            "notes": {"type": "string"},
                        },
                        "required": ["score"],
                        "additionalProperties": False,
                    },
                    "allowed_update_fields": ["score", "notes"],
                    "required_evidence": True,
                    "required_position_keys": ["sales-analyst"],
                    "required_capabilities": ["crm.qualify"],
                },
                "annotate": {
                    "from": ["QUALIFIED"],
                    "to": "QUALIFIED",
                    "input_schema": {
                        "type": "object",
                        "properties": {"notes": {"type": "string"}},
                        "required": ["notes"],
                        "additionalProperties": False,
                    },
                    "allowed_update_fields": ["notes"],
                },
            },
        },
        sensitive_fields=["email"],
        ownership_rules={"position_required": True},
        retention_policy={"days": 365},
    )
    return service.transition_type(company_id, value.id, "publish")


def test_business_objects_require_explicit_feature_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = BusinessObjectService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )

    with pytest.raises(FeatureDisabled, match="business_objects"):
        service.list_types(next(iter(uow_factory.store.companies), None))


def test_named_action_appends_revision_redacts_sensitive_data_and_rejects_stale_write(
    company_service: CompanyModelService,
    business_object_service: BusinessObjectService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, position = _company(company_service)
    lead_type = _lead_type(business_object_service, company.id)
    created = business_object_service.create_object(
        company.id,
        type_id=lead_type.id,
        data={"name": "Ada", "email": "ada@example.test"},
        external_ref="crm-42",
        owner_position_id=position.id,
        actor="owner",
        source_type=ObjectSourceType.IMPORT,
        source_id="fixture-import",
    )
    with pytest.raises(BusinessObjectConflict, match="requires evidence"):
        business_object_service.apply_action(
            company.id,
            created.object.id,
            action_key="qualify",
            expected_revision=1,
            input={"score": 95},
            actor="agent:test",
            actor_position_key="sales-analyst",
            actor_capabilities=["crm.qualify"],
        )
    qualified = business_object_service.apply_action(
        company.id,
        created.object.id,
        action_key="qualify",
        expected_revision=1,
        input={"score": 95, "notes": "Verified fit"},
        actor="agent:test",
        source_type=ObjectSourceType.AGENT,
        source_id="task:fixture",
        evidence_refs=["artifact:qualification"],
        actor_position_key="sales-analyst",
        actor_capabilities=["crm.qualify"],
    )

    with pytest.raises(InvalidBusinessObject, match="Stale"):
        business_object_service.apply_action(
            company.id,
            created.object.id,
            action_key="annotate",
            expected_revision=1,
            input={"notes": "Stale overwrite"},
            actor="owner",
        )

    assert qualified.object.current_revision == 2
    assert qualified.object.lifecycle_state == "QUALIFIED"
    assert [revision.revision for revision in qualified.revisions] == [1, 2]
    assert qualified.revisions[0].data["email"] == "***REDACTED***"
    assert qualified.revisions[1].data["email"] == "***REDACTED***"
    assert qualified.revisions[1].data_digest == uow_factory.store.business_object_revisions[
        (created.object.id, 2)
    ].data_digest
    assert (
        uow_factory.store.business_object_revisions[(created.object.id, 2)].data[
            "email"
        ]
        == "ada@example.test"
    )
    assert all(
        "email" not in event.payload
        for event in uow_factory.store.outbox
        if event.schema_name.startswith("agentmesh.company.object.")
    )


def test_type_versions_are_immutable_and_new_publish_deprecates_previous(
    company_service: CompanyModelService,
    business_object_service: BusinessObjectService,
) -> None:
    company, _ = _company(company_service)
    first = _lead_type(business_object_service, company.id, schema_version=1)
    second = _lead_type(business_object_service, company.id, schema_version=2)
    values = business_object_service.list_types(company.id)

    assert first.status is BusinessObjectTypeStatus.PUBLISHED
    assert second.status is BusinessObjectTypeStatus.PUBLISHED
    assert next(value for value in values if value.id == first.id).status is (
        BusinessObjectTypeStatus.DEPRECATED
    )
    assert next(value for value in values if value.id == second.id).status is (
        BusinessObjectTypeStatus.PUBLISHED
    )
    assert first.content_digest != second.content_digest


def test_business_object_schema_and_action_boundaries_fail_closed(
    company_service: CompanyModelService,
    business_object_service: BusinessObjectService,
) -> None:
    company, position = _company(company_service)
    lead_type = _lead_type(business_object_service, company.id)
    with pytest.raises(InvalidBusinessObject, match="failed"):
        business_object_service.create_object(
            company.id,
            type_id=lead_type.id,
            data={"name": "Missing email"},
            owner_position_id=position.id,
            actor="owner",
        )
    created = business_object_service.create_object(
        company.id,
        type_id=lead_type.id,
        data={"name": "Lin", "email": "lin@example.test"},
        owner_position_id=position.id,
        actor="owner",
    )
    with pytest.raises(BusinessObjectConflict, match="allowed Position"):
        business_object_service.apply_action(
            company.id,
            created.object.id,
            action_key="qualify",
            expected_revision=1,
            input={"score": 90},
            actor="agent:test",
            evidence_refs=["artifact:score"],
            actor_position_key="engineer",
            actor_capabilities=["crm.qualify"],
        )


def test_business_object_api_exposes_redacted_revision_timeline(
    application_container: ApplicationContainer,
    company_service: CompanyModelService,
) -> None:
    company, position = _company(company_service)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "company_model=true,business_objects=true"
    )
    with TestClient(create_app(application_container)) as client:
        created_type = client.post(
            f"/api/v1/companies/{company.id}/business-object-types",
            json={
                "key": "customer",
                "name": "Customer",
                "schema_version": 1,
                "json_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["name", "email"],
                    "additionalProperties": False,
                },
                "lifecycle_definition": {
                    "states": ["ACTIVE"],
                    "initial_state": "ACTIVE",
                    "actions": {},
                },
                "sensitive_fields": ["email"],
            },
        )
        assert created_type.status_code == 201
        type_id = created_type.json()["id"]
        assert client.post(
            f"/api/v1/companies/{company.id}/business-object-types/{type_id}/transition",
            json={"action": "publish"},
        ).status_code == 200
        created = client.post(
            f"/api/v1/companies/{company.id}/business-objects",
            json={
                "type_id": type_id,
                "data": {"name": "API Customer", "email": "secret@example.test"},
                "owner_position_id": str(position.id),
            },
        )
        assert created.status_code == 201
        assert created.json()["revisions"][0]["data"]["email"] == "***REDACTED***"
        object_id = created.json()["object"]["id"]
        snapshot = client.get(
            f"/api/v1/companies/{company.id}/business-objects/{object_id}"
        )
        assert snapshot.status_code == 200
        assert snapshot.json()["revisions"][0]["data"]["email"] == "***REDACTED***"
