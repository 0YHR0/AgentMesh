from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.bootstrap import ApplicationContainer


def test_direct_task_api_accepts_and_persists_preferred_agent(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Use the selected Direct employee",
                "preferred_agent_id": "test-agent",
            },
        )
        assert created.status_code == 201
        task = created.json()
        selection = task["input"]["agentmesh_execution"]
        assert selection["preferred_agent_id"] == "test-agent"
        assert selection["preferred_agent_version_id"]
        assert selection["preferred_agent_version_digest"]

        requested = client.post(f"/api/v1/tasks/{task['id']}/runs")
        assert requested.status_code == 202
        run = requested.json()["runs"][0]
        assert run["agent_id"] == "test-agent"
        assert run["agent_version_id"] == selection["preferred_agent_version_id"]
        assert run["agent_version_digest"] == selection["preferred_agent_version_digest"]


def test_non_direct_task_api_rejects_preferred_agent(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Do not override a reviewed task's executor",
                "execution_mode": "REVIEWED",
                "preferred_agent_id": "test-agent",
            },
        )
        assert response.status_code == 422
        assert "only valid for DIRECT" in response.json()["detail"][0]["msg"]
