import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_operation_services import CompanyOperationService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.services import TaskApplicationService
from agentmesh.config import get_settings
from agentmesh.domain.company_operations import MissedSchedulePolicy, TriggerKind
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_company_operation_claim_and_task_lineage_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"company-operations-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,company_goals=true,company_operations=true",
    )
    task_service = TaskApplicationService(
        uow_factory=factory,
        agent_id="integration-agent",
        tenant_id=tenant_id,
        feature_gates=gates,
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    operation_service = CompanyOperationService(
        uow_factory=factory,
        task_service=task_service,
        tenant_id=tenant_id,
        feature_gates=gates,
    )
    try:
        company = company_service.create_company(
            name="Operations Integration Company",
            mission="Prove durable schedule claims and Task lineage.",
            owner_principal_id="integration-owner",
        )
        unit = company_service.create_unit(
            company.id,
            key="operations",
            name="Operations",
            kind="department",
            purpose="Run recurring work.",
        )
        activated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        operation = operation_service.create_operation(
            company.id,
            organization_unit_id=unit.id,
            key="integration-operation",
            name="Integration Operation",
            objective_template="Persist one scheduled Task.",
            input_template={"fixture": True},
            trigger_kind=TriggerKind.INTERVAL,
            trigger_definition={"interval_seconds": 60},
            timezone="UTC",
            missed_policy=MissedSchedulePolicy.LATEST,
            catch_up_limit=2,
        )
        operation_service.transition_operation(
            company.id, operation.id, "activate", now=activated_at
        )

        first = factory()
        second = factory()
        first.__enter__()
        second.__enter__()
        try:
            assert (
                len(
                    first.company_operations.list_due(
                        datetime.now(timezone.utc), tenant_id=tenant_id, limit=1
                    )
                )
                == 1
            )
            assert second.company_operations.list_due(
                datetime.now(timezone.utc), tenant_id=tenant_id, limit=1
            ) == []
        finally:
            first.rollback()
            second.rollback()
            first.__exit__(None, None, None)
            second.__exit__(None, None, None)

        launches = operation_service.dispatch_due(now=datetime.now(timezone.utc))
        assert len(launches) == 1
        task_id = launches[0].task.task.id if launches[0].task else None
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT o.status, s.fencing_token, x.status, x.task_id "
                    "FROM company_operations o "
                    "JOIN company_operation_trigger_states s ON s.operation_id = o.id "
                    "JOIN company_operation_occurrences x ON x.operation_id = o.id "
                    "WHERE o.id = :operation_id AND x.status = 'TASK_CREATED'"
                ),
                {"operation_id": operation.id},
            ).one()
        assert row == ("ACTIVE", 1, "TASK_CREATED", task_id)
    finally:
        engine.dispose()
