from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.deliverable_acceptance import project_deliverable_acceptance
from agentmesh.domain.deliverable_acceptance import ACCEPTANCE_POLICY_INPUT_KEY
from agentmesh.domain.errors import IdempotencyConflict, InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.identity import Role
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from tests.test_identity_rbac import _headers, _principal, _secured_container
from tests.test_output_policies import _plan


def _policy(*, human=True):
    return {
        "require_human_review": human,
        "checks": [
            {
                "key": "summary",
                "description": "Result has a summary",
                "kind": "OUTPUT_PATH_EXISTS",
                "path": ["summary"],
                "required": True,
            }
        ],
    }


def _completed(task_service, uow_factory, *, human=True, output=None, key=None):
    created = task_service.create_task(
        "Deliver a report",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=_plan(),
        acceptance_policy=_policy(human=human),
        idempotency_key=key,
    )
    uow_factory.store.tasks[created.task.id].status = TaskStatus.COMPLETED
    for subtask in uow_factory.store.subtasks.values():
        if subtask.task_id == created.task.id:
            subtask.output = output if output is not None else {"summary": "A complete report"}
            from agentmesh.domain.coordination import SubtaskStatus

            subtask.status = SubtaskStatus.COMPLETED
    return task_service.get_task(created.task.id)


def _request(aggregate, **kwargs):
    acceptance = project_deliverable_acceptance(aggregate)
    return {
        "decision": "ACCEPT",
        "actor": "operator-1",
        "reason": "Reviewed evidence",
        "expected_policy_digest": acceptance["policy_digest"],
        "expected_deliverable_digest": acceptance["deliverable_digest"],
        **kwargs,
    }


def test_creation_is_default_off_and_rejects_reserved_input(task_service):
    assert (
        project_deliverable_acceptance(task_service.create_task("Simple task"))["status"]
        == "NOT_CONFIGURED"
    )
    with pytest.raises(InvalidTaskInput, match="server-managed"):
        task_service.create_task("Spoof policy", input={ACCEPTANCE_POLICY_INPUT_KEY: _policy()})
    with pytest.raises(InvalidTaskInput):
        task_service.create_task("Wrong execution mode", acceptance_policy=_policy())


def test_decisions_are_audited_idempotent_without_changing_execution(
    task_service,
    resolution_service,
    uow_factory,
):
    aggregate = _completed(task_service, uow_factory, key="creation")
    request = _request(aggregate, idempotency_key="accept")
    first = resolution_service.decide_deliverable(aggregate.task.id, **request)
    replay = resolution_service.decide_deliverable(aggregate.task.id, **request)
    assert replay.resolution.id == first.resolution.id
    assert first.resolution.action is TaskResolutionAction.ACCEPT_DELIVERABLE
    assert first.aggregate.task.status is TaskStatus.COMPLETED
    assert project_deliverable_acceptance(first.aggregate)["status"] == "HUMAN_ACCEPTED"
    assert project_deliverable_acceptance(task_service.list_tasks()[0])["delivery_allowed"]
    recreated = task_service.create_task(
        "Deliver a report",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=_plan(),
        acceptance_policy=_policy(),
        idempotency_key="creation",
    )
    assert project_deliverable_acceptance(recreated)["status"] == "HUMAN_ACCEPTED"
    with pytest.raises(IdempotencyConflict):
        resolution_service.decide_deliverable(
            aggregate.task.id,
            **{
                **request,
                "reason": "Changed reason",
            },
        )
    rejected = resolution_service.decide_deliverable(
        aggregate.task.id,
        **_request(first.aggregate, decision="REJECT"),
    )
    assert project_deliverable_acceptance(rejected.aggregate)["status"] == "HUMAN_REJECTED"
    assert rejected.aggregate.task.status is TaskStatus.COMPLETED


def test_decision_rejects_stale_or_invalid_evidence(task_service, resolution_service, uow_factory):
    aggregate = _completed(task_service, uow_factory)
    with pytest.raises(InvalidTaskTransition, match="changed"):
        resolution_service.decide_deliverable(
            aggregate.task.id,
            **_request(aggregate, expected_deliverable_digest="0" * 64),
        )
    uow_factory.store.tasks[aggregate.task.id].input["invalid_fact"] = float("nan")
    with pytest.raises(InvalidTaskTransition, match="not valid"):
        resolution_service.decide_deliverable(aggregate.task.id, **_request(aggregate))
    assert not uow_factory.store.task_resolutions


def test_api_gates_export_and_rejects_actor_spoof(
    task_service,
    uow_factory,
    application_container,
):
    aggregate = _completed(task_service, uow_factory)
    url = f"/api/v1/tasks/{aggregate.task.id}"
    payload = _request(aggregate)
    payload.pop("actor")
    with TestClient(create_app(application_container)) as client:
        assert client.get(f"{url}/accepted-deliverable").status_code == 409
        assert (
            client.post(
                f"{url}/deliverable-acceptance/decision",
                json={
                    **payload,
                    "actor": "forged-admin",
                },
            ).status_code
            == 422
        )
        accepted = client.post(
            f"{url}/deliverable-acceptance/decision",
            json=payload,
            headers={"Idempotency-Key": "api-accept-deliverable"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["resolution"]["actor"] == "operator"
        assert accepted.json()["task"]["deliverable_acceptance"]["status"] == "HUMAN_ACCEPTED"
        replay = client.post(
            f"{url}/deliverable-acceptance/decision",
            json=payload,
            headers={"Idempotency-Key": "api-accept-deliverable"},
        )
        assert replay.status_code == 200
        assert replay.json()["resolution"]["id"] == accepted.json()["resolution"]["id"]
        result = client.get(f"{url}/accepted-deliverable")
        assert result.status_code == 200
        assert result.json()["subtask_key"] == "report"
        assert result.json()["output"]["summary"] == "A complete report"
    disabled = replace(application_container, feature_gates=FeatureGateSet.from_config("minimal"))
    with TestClient(create_app(disabled)) as client:
        response = client.post(f"{url}/deliverable-acceptance/decision", json=payload)
        assert response.status_code == 403


def test_api_automatic_pass_and_unconfigured_export(
    task_service,
    uow_factory,
    application_container,
):
    aggregate = _completed(task_service, uow_factory, human=False)
    direct = task_service.create_task("No acceptance")
    with TestClient(create_app(application_container)) as client:
        assert (
            client.get(f"/api/v1/tasks/{aggregate.task.id}/accepted-deliverable").status_code == 200
        )
        assert client.get(f"/api/v1/tasks/{direct.task.id}/accepted-deliverable").status_code == 409


def test_acceptance_api_requires_auth_permission_and_uses_principal_actor(
    task_service,
    uow_factory,
    application_container,
):
    aggregate = _completed(task_service, uow_factory)
    container = _secured_container(
        application_container,
        _principal("operator", Role.OPERATOR),
        _principal("auditor", Role.AUDITOR),
    )
    url = f"/api/v1/tasks/{aggregate.task.id}/deliverable-acceptance/decision"
    payload = _request(aggregate)
    payload.pop("actor")
    with TestClient(create_app(container)) as client:
        assert client.post(url, json=payload).status_code == 401
        assert client.post(url, json=payload, headers=_headers("auditor")).status_code == 403
        accepted = client.post(url, json=payload, headers=_headers("operator"))
        assert accepted.status_code == 200
        assert accepted.json()["resolution"]["actor"] == "operator"


def test_creation_api_pins_policy_and_rejects_ambiguous_primary(application_container):
    body = {
        "objective": "Create acceptance task",
        "execution_mode": "COORDINATED",
        "acceptance_policy": _policy(),
        "subtasks": [
            {"key": "research", "objective": "Research"},
            {"key": "report", "objective": "Report", "depends_on": ["research"]},
        ],
    }
    with TestClient(create_app(application_container)) as client:
        created = client.post("/api/v1/tasks", json=body)
        assert created.status_code == 201
        data = created.json()
        assert data["input"][ACCEPTANCE_POLICY_INPUT_KEY]["target_subtask_key"] == "report"
        assert data["deliverable_acceptance"]["status"] == "NOT_READY"
        body["subtasks"].append({"key": "media", "objective": "Media"})
        assert client.post("/api/v1/tasks", json=body).status_code == 422
