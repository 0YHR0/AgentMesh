import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.activity_services import TaskActivityService
from agentmesh.application.artifact_services import ArtifactService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.artifacts import ArtifactClassification
from agentmesh.domain.coordination import Subtask
from agentmesh.domain.errors import TaskNotFound
from agentmesh.domain.handoffs import Handoff
from agentmesh.domain.policy import GovernedAction, GovernedActionType, PolicyResult
from agentmesh.domain.tasks import Task, TaskRun, utc_now
from agentmesh.domain.tools import ToolBinding, ToolInvocation, ToolSideEffect
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


def test_interaction_projection_exposes_topology_but_redacts_payloads(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task = Task.create(tenant_id="test-tenant", objective="private objective")
    source = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="research",
        objective="secret research",
        input={"secret": "must-not-leak"},
        required_capabilities=("web.research",),
        preferred_agent_id="research-agent",
        initially_ready=True,
    )
    target = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="review",
        objective="secret review",
        input={},
        required_capabilities=("review",),
        preferred_agent_id="review-agent",
        initially_ready=False,
    )
    run = TaskRun.request(task.id, "research-agent", subtask_id=source.id)
    handoff = Handoff.request(
        task_id=task.id,
        source_subtask_id=source.id,
        source_run_id=run.id,
        source_trace_id="a" * 32,
        source_agent_id="research-agent",
        target_subtask_id=target.id,
        target_agent_id="review-agent",
        objective="private handoff objective",
        reason="private handoff reason",
        completed_work_summary="private completed work",
        requested_by="research-agent",
    )
    invocation = ToolInvocation.start(
        tenant_id="test-tenant",
        task_id=task.id,
        run_id=run.id,
        binding=ToolBinding(
            logical_key="workspace.read_text",
            server_name="workspace",
            tool_name="read_text",
            side_effect=ToolSideEffect.READ_ONLY,
        ),
        arguments={"path": "must-not-leak"},
    )
    action = GovernedAction.create(
        tenant_id="test-tenant",
        requester_id="operator",
        action_type=GovernedActionType.A2A_DELEGATE,
        resource_type="task",
        resource_id=task.id,
        arguments={"remote_prompt": "must-not-leak"},
        policy_result=PolicyResult.REQUIRE_APPROVAL,
        reason_code="approval_required",
        policy_bundle="default",
        policy_version="1",
        created_at=utc_now(),
        expires_at=utc_now() + timedelta(minutes=5),
    )
    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.subtasks.add(source)
        uow.subtasks.add(target)
        uow.runs.add(run)
        uow.handoffs.add(handoff)
        uow.tool_invocations.add(invocation)
        uow.policy.add_action(action)
        uow.commit()

    events = TaskActivityService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    ).interactions(task.id, limit=100)
    serialized = json.dumps([event.__dict__ for event in events], default=str)

    assert {event.transport for event in events} >= {"HANDOFF", "MCP", "POLICY"}
    assert any(
        event.source.id == str(source.id) and event.target.id == str(target.id)
        for event in events
    )
    assert any(event.target.type == "TOOL" for event in events)
    assert any(event.target.type == "APPROVAL" for event in events)
    assert "must-not-leak" not in serialized
    assert "private handoff" not in serialized


def test_interaction_api_is_bounded_and_tenant_scoped(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"objective": "Inspect governed interactions", "input": {}},
        )
        task_id = created.json()["id"]
        response = client.get(f"/api/v1/tasks/{task_id}/interactions?limit=1")

        assert response.status_code == 200
        assert response.json() == {"task_id": task_id, "items": [], "limit": 1}

    foreign_service = TaskActivityService(
        uow_factory=uow_factory,
        tenant_id="another-tenant",
    )
    with pytest.raises(TaskNotFound):
        foreign_service.interactions(UUID(task_id), limit=100)
