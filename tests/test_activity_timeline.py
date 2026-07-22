import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.activity_services import TaskActivityService
from agentmesh.application.artifact_services import ArtifactService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.artifacts import ArtifactClassification
from agentmesh.domain.errors import TaskNotFound
from agentmesh.domain.tasks import Task, TaskRun
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def test_activity_timeline_normalizes_ledgers_without_domain_payloads(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task = Task.create(
        tenant_id="test-tenant",
        objective="private objective",
        input={"secret": "must-not-leak"},
    )
    run = TaskRun.request(task.id, "research-agent")
    run.start()
    run.succeed({"private-result": "must-not-leak"})
    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.runs.add(run)
        uow.commit()
    ArtifactService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        owner_id="operator",
        max_inline_bytes=65_536,
    ).create_artifact(
        display_name="evidence.json",
        kind="task.evidence",
        classification=ArtifactClassification.INTERNAL,
        media_type="application/json",
        content=b'{"private-artifact":"must-not-leak"}',
        producer_run_id=run.id,
    )

    events = TaskActivityService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    ).timeline(task.id, limit=100)
    serialized = json.dumps([event.__dict__ for event in events], default=str)

    assert [event.occurred_at for event in events] == sorted(
        [event.occurred_at for event in events], reverse=True
    )
    assert {event.category for event in events} >= {"task", "run", "artifact"}
    assert "private objective" not in serialized
    assert "must-not-leak" not in serialized
    assert "private-result" not in serialized
    assert "private-artifact" not in serialized


def test_activity_timeline_is_tenant_scoped(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task = Task.create(tenant_id="test-tenant", objective="tenant private")
    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.commit()

    service = TaskActivityService(uow_factory=uow_factory, tenant_id="another-tenant")

    with pytest.raises(TaskNotFound):
        service.timeline(task.id, limit=100)


def test_activity_api_is_bounded_and_feature_gated(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"objective": "Inspect unified activity", "input": {}},
        )
        response = client.get(f"/api/v1/tasks/{created.json()['id']}/activity?limit=1")

        assert response.status_code == 200
        assert response.json()["limit"] == 1
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["category"] == "task"

    disabled = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(disabled)) as client:
        response = client.get(f"/api/v1/tasks/{created.json()['id']}/activity")
        assert response.status_code == 403
        assert response.json()["code"] == "feature_disabled"
