from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_operation_services import CompanyOperationService
from agentmesh.application.company_services import CompanyModelService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.company_operations import (
    MissedSchedulePolicy,
    OccurrenceStatus,
    OperationStatus,
    TriggerKind,
)
from agentmesh.domain.errors import FeatureDisabled, IdempotencyConflict
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _company(company_service: CompanyModelService):
    company = company_service.create_company(
        name="Operations Company",
        mission="Turn approved recurring work into bounded Tasks.",
        owner_principal_id="owner",
    )
    unit = company_service.create_unit(
        company.id,
        key="operations",
        name="Operations",
        kind="department",
        purpose="Run repeatable operating loops.",
    )
    return company, unit


def _operation(
    service: CompanyOperationService,
    company_id,
    unit_id,
    *,
    key: str = "daily-report",
    missed_policy: MissedSchedulePolicy = MissedSchedulePolicy.LATEST,
    concurrency_limit: int = 1,
    activated_at: datetime,
):
    operation = service.create_operation(
        company_id,
        organization_unit_id=unit_id,
        key=key,
        name="Daily report",
        objective_template="Produce the scheduled operating report.",
        input_template={"format": "brief"},
        trigger_kind=TriggerKind.INTERVAL,
        trigger_definition={"interval_seconds": 60},
        timezone="UTC",
        missed_policy=missed_policy,
        catch_up_limit=2,
        concurrency_limit=concurrency_limit,
    )
    return service.transition_operation(
        company_id, operation.id, "activate", now=activated_at
    )


def test_company_operations_require_explicit_feature_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
    task_service,
) -> None:
    service = CompanyOperationService(
        uow_factory=uow_factory,
        task_service=task_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,company_goals=true"
        ),
    )

    with pytest.raises(FeatureDisabled, match="company_operations"):
        service.dispatch_due()


def test_due_operation_creates_one_traceable_task_on_redelivery(
    company_service: CompanyModelService,
    company_operation_service: CompanyOperationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, unit = _company(company_service)
    activated_at = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    operation = _operation(
        company_operation_service,
        company.id,
        unit.id,
        activated_at=activated_at,
    )

    first = company_operation_service.dispatch_due(
        now=activated_at + timedelta(seconds=60)
    )
    replay = company_operation_service.dispatch_due(
        now=activated_at + timedelta(seconds=60)
    )
    snapshot = company_operation_service.get_operation(company.id, operation.id)

    assert len(first) == 1
    assert replay == []
    assert first[0].occurrence.status is OccurrenceStatus.TASK_CREATED
    assert first[0].task is not None
    assert len(uow_factory.store.tasks) == 1
    context = first[0].task.task.input["company_context"]
    assert context["operation_id"] == str(operation.id)
    assert context["operation_digest"] == operation.content_digest
    assert snapshot.trigger_state is not None
    assert snapshot.trigger_state.fencing_token == 1
    assert snapshot.occurrences[0].task_id == first[0].task.task.id


def test_manual_event_id_is_idempotent_even_when_redelivered_at_another_time(
    company_service: CompanyModelService,
    company_operation_service: CompanyOperationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, unit = _company(company_service)
    operation = company_operation_service.create_operation(
        company.id,
        organization_unit_id=unit.id,
        key="manual-review",
        name="Manual review",
        objective_template="Review the imported business event.",
        input_template={},
        trigger_kind=TriggerKind.MANUAL,
        trigger_definition={},
        timezone="UTC",
        missed_policy=MissedSchedulePolicy.LATEST,
    )
    operation = company_operation_service.transition_operation(
        company.id, operation.id, "activate"
    )
    first = company_operation_service.trigger_manual(
        company.id,
        operation.id,
        event_id="webhook-42",
        scheduled_at=datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
    )
    replay = company_operation_service.trigger_manual(
        company.id,
        operation.id,
        event_id="webhook-42",
        scheduled_at=datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc),
    )

    assert first.task is not None
    assert replay.task is not None
    assert replay.task.task.id == first.task.task.id
    assert replay.occurrence.id == first.occurrence.id
    assert len(uow_factory.store.tasks) == 1


def test_missed_schedule_latest_records_skipped_evidence(
    company_service: CompanyModelService,
    company_operation_service: CompanyOperationService,
) -> None:
    company, unit = _company(company_service)
    activated_at = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    operation = _operation(
        company_operation_service,
        company.id,
        unit.id,
        key="latest-only",
        missed_policy=MissedSchedulePolicy.LATEST,
        activated_at=activated_at,
    )

    launches = company_operation_service.dispatch_due(
        now=activated_at + timedelta(minutes=3)
    )
    snapshot = company_operation_service.get_operation(company.id, operation.id)

    assert len(launches) == 1
    assert launches[0].occurrence.scheduled_at == activated_at + timedelta(minutes=3)
    assert {item.status for item in snapshot.occurrences} == {
        OccurrenceStatus.SKIPPED,
        OccurrenceStatus.TASK_CREATED,
    }


@pytest.mark.parametrize(
    ("policy", "expected_launches", "expected_status", "paused"),
    [
        (MissedSchedulePolicy.SKIP, 0, OccurrenceStatus.SKIPPED, False),
        (
            MissedSchedulePolicy.CATCH_UP_BOUNDED,
            2,
            OccurrenceStatus.TASK_CREATED,
            False,
        ),
        (
            MissedSchedulePolicy.REQUIRE_REVIEW,
            0,
            OccurrenceStatus.REVIEW_REQUIRED,
            True,
        ),
    ],
)
def test_missed_schedule_policies_are_bounded_and_evidenced(
    company_service: CompanyModelService,
    company_operation_service: CompanyOperationService,
    policy: MissedSchedulePolicy,
    expected_launches: int,
    expected_status: OccurrenceStatus,
    paused: bool,
) -> None:
    company, unit = _company(company_service)
    activated_at = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    operation = _operation(
        company_operation_service,
        company.id,
        unit.id,
        key=f"policy-{policy.value.lower()}",
        missed_policy=policy,
        concurrency_limit=2,
        activated_at=activated_at,
    )

    launches = company_operation_service.dispatch_due(
        now=activated_at + timedelta(hours=10)
    )
    snapshot = company_operation_service.get_operation(company.id, operation.id)

    assert len(launches) == expected_launches
    assert snapshot.occurrences[0].status is expected_status
    assert (snapshot.operation.status is OperationStatus.PAUSED) is paused
    assert len(snapshot.occurrences) <= 2


def test_failed_task_creation_is_visible_as_operation_exception(
    company_service: CompanyModelService,
    company_operation_service: CompanyOperationService,
    monkeypatch,
) -> None:
    company, unit = _company(company_service)
    operation = company_operation_service.create_operation(
        company.id,
        organization_unit_id=unit.id,
        key="failing-operation",
        name="Fail visibly",
        objective_template="This Task creation is forced to fail.",
        input_template={},
        trigger_kind=TriggerKind.MANUAL,
        trigger_definition={},
        timezone="UTC",
        missed_policy=MissedSchedulePolicy.LATEST,
    )
    company_operation_service.transition_operation(
        company.id, operation.id, "activate"
    )

    def fail(*args, **kwargs):
        raise IdempotencyConflict("forced collision")

    monkeypatch.setattr(company_operation_service._task_service, "create_task", fail)
    for _ in range(3):
        with pytest.raises(IdempotencyConflict, match="forced collision"):
            company_operation_service.trigger_manual(
                company.id, operation.id, event_id="collision"
            )
    snapshot = company_operation_service.get_operation(company.id, operation.id)

    assert snapshot.occurrences[0].status is OccurrenceStatus.FAILED
    assert snapshot.exceptions[0].code == "TASK_CREATION_FAILED"
    assert snapshot.exceptions[0].retryable is False
    assert snapshot.occurrences[0].detail["attempts"] == 3


def test_company_operation_api_exposes_lifecycle_trigger_and_evidence(
    application_container: ApplicationContainer,
    company_service: CompanyModelService,
) -> None:
    company, unit = _company(company_service)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,company_goals=true,company_operations=true",
    )
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            f"/api/v1/companies/{company.id}/operations",
            json={
                "organization_unit_id": str(unit.id),
                "key": "api-operation",
                "name": "API Operation",
                "objective_template": "Create an API-triggered Task.",
                "trigger_kind": "MANUAL",
                "missed_policy": "LATEST",
            },
        )
        assert created.status_code == 201
        operation_id = created.json()["id"]
        activated = client.post(
            f"/api/v1/companies/{company.id}/operations/{operation_id}/transition",
            json={"action": "activate"},
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == OperationStatus.ACTIVE.value
        launched = client.post(
            f"/api/v1/companies/{company.id}/operations/{operation_id}/trigger",
            json={"event_id": "api-event"},
        )
        assert launched.status_code == 200
        assert launched.json()["occurrence"]["status"] == "TASK_CREATED"
        snapshot = client.get(
            f"/api/v1/companies/{company.id}/operations/{operation_id}"
        )
        assert snapshot.status_code == 200
        assert (
            snapshot.json()["occurrences"][0]["task_id"]
            == launched.json()["task"]["id"]
        )
