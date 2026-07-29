from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_goal_services import CompanyGoalService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.company_goals import (
    InitiativeStatus,
    ObjectiveStatus,
    OperatingCycleStatus,
)
from agentmesh.domain.errors import CompanyGoalConflict, FeatureDisabled, InvalidCompanyGoal
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _organization(company_service: CompanyModelService):
    company = company_service.create_company(
        name="Goal Company",
        mission="Turn approved goals into evidence-backed Tasks.",
        owner_principal_id="owner",
    )
    unit = company_service.create_unit(
        company.id,
        key="product",
        name="Product",
        kind="department",
        purpose="Own product outcomes.",
    )
    position = company_service.create_position(
        company.id,
        primary_unit_id=unit.id,
        key="product-owner",
        title="Product Owner",
        responsibility_contract={"outcomes": ["verified product result"]},
    )
    return company, unit, position


def _active_cycle(
    service: CompanyGoalService,
    company_id,
    *,
    actor: str = "owner",
):
    now = datetime.now(timezone.utc)
    cycle = service.create_cycle(
        company_id,
        name="Q3",
        starts_at=now,
        ends_at=now + timedelta(days=90),
        review_schedule={"cadence": "weekly"},
    )
    service.transition_cycle(company_id, cycle.id, "approve", actor)
    return service.transition_cycle(company_id, cycle.id, "activate", actor)


def test_company_goals_are_disabled_without_explicit_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
    task_service,
) -> None:
    service = CompanyGoalService(
        uow_factory=uow_factory,
        task_service=task_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )
    with pytest.raises(FeatureDisabled, match="company_goals"):
        service.list_cycles(next(iter(uow_factory.store.companies), None))


def test_goal_hierarchy_launches_traceable_task_and_separates_measurements(
    company_service: CompanyModelService,
    company_goal_service: CompanyGoalService,
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
    task_service,
    execution_service,
) -> None:
    company, unit, position = _organization(company_service)
    cycle = _active_cycle(company_goal_service, company.id)
    target_date = cycle.ends_at - timedelta(days=7)
    objective = company_goal_service.create_objective(
        company.id,
        cycle.id,
        owner_position_id=position.id,
        statement="Validate one repeatable customer outcome.",
        rationale="A bounded outcome proves the operating loop.",
        priority=1,
        target_date=target_date,
    )
    company_goal_service.transition_objective(company.id, objective.id, "approve")
    active_objective = company_goal_service.transition_objective(
        company.id, objective.id, "activate"
    )
    key_result = company_goal_service.create_key_result(
        company.id,
        objective.id,
        metric_key="accepted-deliverables",
        unit="count",
        baseline="0",
        target="1.0",
        measurement_source="artifact-acceptance-ledger",
    )
    estimated = company_goal_service.record_key_result(
        company.id,
        key_result.id,
        value="0.75",
        verified=False,
    )
    verified = company_goal_service.record_key_result(
        company.id,
        key_result.id,
        value="1",
        verified=True,
        source="accepted-artifact:fixture",
    )
    initiative = company_goal_service.create_initiative(
        company.id,
        objective.id,
        owner_unit_id=unit.id,
        title="Produce the deterministic deliverable",
        outcome_contract={
            "acceptance_criteria": ["artifact accepted"],
            "evidence": ["task", "artifact"],
        },
    )
    company_goal_service.transition_initiative(company.id, initiative.id, "approve")
    active_initiative = company_goal_service.transition_initiative(
        company.id, initiative.id, "activate"
    )
    launch = company_goal_service.launch_task(
        company.id,
        initiative.id,
        objective="Produce and verify the bounded deliverable.",
        input={"brief": "deterministic fixture"},
        created_by="owner",
    )
    with pytest.raises(CompanyGoalConflict, match="completed Task evidence"):
        company_goal_service.transition_initiative(
            company.id, initiative.id, "complete"
        )
    task_service.request_run(launch.task.task.id, idempotency_key="initiative-run")
    assert execution_service.process(uow_factory.store.outbox[-1])
    completed = company_goal_service.transition_initiative(
        company.id, initiative.id, "complete"
    )
    snapshot = company_goal_service.get_cycle(company.id, cycle.id)

    assert cycle.status is OperatingCycleStatus.ACTIVE
    assert active_objective.status is ObjectiveStatus.ACTIVE
    assert active_initiative.status is InitiativeStatus.ACTIVE
    assert completed.status is InitiativeStatus.COMPLETED
    assert estimated.current_estimated_value == "0.75"
    assert verified.current_estimated_value == "0.75"
    assert verified.current_verified_value == "1"
    assert launch.task.task.input["company_context"]["initiative_id"] == str(initiative.id)
    assert launch.link.task_id == launch.task.task.id
    assert snapshot.objectives[0].task_links[initiative.id][0].task_id == launch.task.task.id
    assert launch.task.task.id in uow_factory.store.tasks


def test_goal_lifecycle_rejects_unapproved_work_and_multiple_active_cycles(
    company_service: CompanyModelService,
    company_goal_service: CompanyGoalService,
) -> None:
    company, unit, position = _organization(company_service)
    first = _active_cycle(company_goal_service, company.id)
    now = datetime.now(timezone.utc)
    second = company_goal_service.create_cycle(
        company.id,
        name="Q4",
        starts_at=now + timedelta(days=91),
        ends_at=now + timedelta(days=180),
    )
    company_goal_service.transition_cycle(company.id, second.id, "approve", "owner")
    with pytest.raises(CompanyGoalConflict, match="already has an active"):
        company_goal_service.transition_cycle(
            company.id, second.id, "activate", "owner"
        )

    objective = company_goal_service.create_objective(
        company.id,
        first.id,
        owner_position_id=position.id,
        statement="Proposed only.",
        rationale="Exercise lifecycle admission.",
        priority=2,
        target_date=first.ends_at,
    )
    initiative = company_goal_service.create_initiative(
        company.id,
        objective.id,
        owner_unit_id=unit.id,
        title="Not active",
        outcome_contract={"acceptance_criteria": ["never launched"]},
    )
    with pytest.raises(CompanyGoalConflict, match="Only an active"):
        company_goal_service.launch_task(
            company.id,
            initiative.id,
            objective="Must not launch.",
            input={},
            created_by="owner",
        )
    with pytest.raises(InvalidCompanyGoal, match="requires a source"):
        result = company_goal_service.create_key_result(
            company.id,
            objective.id,
            metric_key="quality",
            unit="basis_points",
            baseline="0",
            target="9000",
            measurement_source="review-ledger",
        )
        company_goal_service.record_key_result(
            company.id, result.id, value="5000", verified=True
        )


def test_company_goal_api_exposes_cycle_snapshot_and_task_lineage(
    application_container: ApplicationContainer,
    company_service: CompanyModelService,
) -> None:
    company, unit, position = _organization(company_service)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "company_model=true,company_goals=true"
    )
    now = datetime.now(timezone.utc)
    with TestClient(create_app(application_container)) as client:
        cycle_response = client.post(
            f"/api/v1/companies/{company.id}/cycles",
            json={
                "name": "API Cycle",
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(days=30)).isoformat(),
            },
        )
        assert cycle_response.status_code == 201
        cycle_id = cycle_response.json()["id"]
        for action in ("approve", "activate"):
            response = client.post(
                f"/api/v1/companies/{company.id}/cycles/{cycle_id}/transition",
                json={"action": action},
            )
            assert response.status_code == 200
        objective_response = client.post(
            f"/api/v1/companies/{company.id}/cycles/{cycle_id}/objectives",
            json={
                "owner_position_id": str(position.id),
                "statement": "API objective",
                "rationale": "Verify nested projection.",
                "priority": 1,
                "target_date": (now + timedelta(days=25)).isoformat(),
            },
        )
        assert objective_response.status_code == 201
        objective_id = objective_response.json()["id"]
        for action in ("approve", "activate"):
            assert client.post(
                f"/api/v1/companies/{company.id}/objectives/{objective_id}/transition",
                json={"action": action},
            ).status_code == 200
        initiative_response = client.post(
            f"/api/v1/companies/{company.id}/objectives/{objective_id}/initiatives",
            json={
                "owner_unit_id": str(unit.id),
                "title": "API initiative",
                "outcome_contract": {"acceptance_criteria": ["Task exists"]},
            },
        )
        initiative_id = initiative_response.json()["id"]
        for action in ("approve", "activate"):
            assert client.post(
                f"/api/v1/companies/{company.id}/initiatives/{initiative_id}/transition",
                json={"action": action},
            ).status_code == 200
        launch = client.post(
            f"/api/v1/companies/{company.id}/initiatives/{initiative_id}/tasks",
            json={"objective": "API linked Task", "input": {"source": "test"}},
        )
        assert launch.status_code == 201
        snapshot = client.get(
            f"/api/v1/companies/{company.id}/cycles/{cycle_id}"
        )
        assert snapshot.status_code == 200
        assert (
            snapshot.json()["objectives"][0]["task_links"][initiative_id][0]["task_id"]
            == launch.json()["task"]["id"]
        )
        office_script = client.get("/console/assets/world3d.js")
        assert office_script.status_code == 200
        assert 'featureEnabled("company_goals")' in office_script.text
        assert "departmentGoalSummary" in office_script.text
        assert "OBJ ·" in office_script.text
