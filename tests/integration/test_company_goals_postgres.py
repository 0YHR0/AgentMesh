import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_goal_services import CompanyGoalService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.services import TaskApplicationService
from agentmesh.config import get_settings
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_company_goal_and_initiative_task_lineage_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"company-goals-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full", "company_model=true,company_goals=true"
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
    goal_service = CompanyGoalService(
        uow_factory=factory,
        task_service=task_service,
        tenant_id=tenant_id,
        feature_gates=gates,
    )
    try:
        company = company_service.create_company(
            name="Goal Integration Company",
            mission="Persist strategy lineage.",
            owner_principal_id="integration-owner",
        )
        unit = company_service.create_unit(
            company.id,
            key="product",
            name="Product",
            kind="department",
            purpose="Own outcomes.",
        )
        position = company_service.create_position(
            company.id,
            primary_unit_id=unit.id,
            key="owner",
            title="Owner",
            responsibility_contract={"outcomes": ["verified result"]},
        )
        now = datetime.now(timezone.utc)
        cycle = goal_service.create_cycle(
            company.id,
            name="Integration Cycle",
            starts_at=now,
            ends_at=now + timedelta(days=30),
        )
        goal_service.transition_cycle(company.id, cycle.id, "approve", "integration-owner")
        goal_service.transition_cycle(company.id, cycle.id, "activate", "integration-owner")
        objective = goal_service.create_objective(
            company.id,
            cycle.id,
            owner_position_id=position.id,
            statement="Create one traceable Task.",
            rationale="Verify public application service linkage.",
            priority=1,
            target_date=now + timedelta(days=20),
        )
        goal_service.transition_objective(company.id, objective.id, "approve")
        goal_service.transition_objective(company.id, objective.id, "activate")
        initiative = goal_service.create_initiative(
            company.id,
            objective.id,
            owner_unit_id=unit.id,
            title="Integration Task",
            outcome_contract={"acceptance_criteria": ["Task persisted"]},
        )
        goal_service.transition_initiative(company.id, initiative.id, "approve")
        goal_service.transition_initiative(company.id, initiative.id, "activate")
        launch = goal_service.launch_task(
            company.id,
            initiative.id,
            objective="Persist this linked Task.",
            input={"fixture": True},
            created_by="integration-owner",
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT oc.status, o.status, i.status, l.task_id "
                    "FROM company_operating_cycles oc "
                    "JOIN company_objectives o ON o.cycle_id = oc.id "
                    "JOIN company_initiatives i ON i.objective_id = o.id "
                    "JOIN company_initiative_tasks l ON l.initiative_id = i.id "
                    "WHERE oc.id = :cycle_id"
                ),
                {"cycle_id": cycle.id},
            ).one()
        assert row == ("ACTIVE", "ACTIVE", "ACTIVE", launch.task.task.id)
    finally:
        engine.dispose()
