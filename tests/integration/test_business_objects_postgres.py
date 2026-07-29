import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.config import get_settings
from agentmesh.domain.errors import InvalidBusinessObject
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_business_object_revision_and_optimistic_lock_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"business-objects-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full", "company_model=true,business_objects=true"
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    object_service = BusinessObjectService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    try:
        company = company_service.create_company(
            name="Business Object Integration Company",
            mission="Persist typed business evidence.",
            owner_principal_id="integration-owner",
        )
        object_type = object_service.create_type(
            company.id,
            key="deliverable",
            name="Deliverable",
            schema_version=1,
            json_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            lifecycle_definition={
                "states": ["DRAFT", "ACCEPTED"],
                "initial_state": "DRAFT",
                "actions": {
                    "accept": {
                        "from": ["DRAFT"],
                        "to": "ACCEPTED",
                        "input_schema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                            "additionalProperties": False,
                        },
                        "allowed_update_fields": ["location"],
                        "required_evidence": True,
                    }
                },
            },
            sensitive_fields=["location"],
        )
        object_service.transition_type(
            company.id, object_type.id, "publish"
        )
        created = object_service.create_object(
            company.id,
            type_id=object_type.id,
            data={"title": "Integration report"},
            actor="integration-owner",
        )
        accepted = object_service.apply_action(
            company.id,
            created.object.id,
            action_key="accept",
            expected_revision=1,
            input={"location": "s3://private/report"},
            actor="integration-owner",
            evidence_refs=["artifact:accepted-report"],
        )
        with pytest.raises(InvalidBusinessObject, match="Stale"):
            object_service.apply_action(
                company.id,
                created.object.id,
                action_key="accept",
                expected_revision=1,
                input={"location": "s3://private/stale"},
                actor="integration-owner",
                evidence_refs=["artifact:stale"],
            )

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT o.current_revision, o.lifecycle_state, r.revision, "
                    "r.action, r.data_digest "
                    "FROM business_objects o "
                    "JOIN business_object_revisions r ON r.object_id = o.id "
                    "WHERE o.id = :object_id ORDER BY r.revision"
                ),
                {"object_id": created.object.id},
            ).all()
        assert [row.revision for row in rows] == [1, 2]
        assert rows[-1].current_revision == 2
        assert rows[-1].lifecycle_state == "ACCEPTED"
        assert rows[-1].data_digest == accepted.revisions[-1].data_digest
        assert accepted.revisions[-1].data["location"] == "***REDACTED***"
    finally:
        engine.dispose()
