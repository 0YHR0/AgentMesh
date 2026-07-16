from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.bootstrap import ApplicationContainer


def test_task_api_vertical_slice(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        health = client.get("/health")
        assert health.status_code == 200

        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Run the AgentMesh demo",
                "input": {"source": "api-test"},
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["status"] == "CREATED"

        completed = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert completed.status_code == 200
        assert completed.json()["status"] == "COMPLETED"
        assert completed.json()["output"]["input"] == {"source": "api-test"}
        assert len(completed.json()["runs"]) == 1

        fetched = client.get(f"/api/v1/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["current_run_id"] == completed.json()["current_run_id"]


def test_duplicate_run_returns_conflict(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post("/api/v1/tasks", json={"objective": "Run once"})
        task_id = created.json()["id"]
        assert client.post(f"/api/v1/tasks/{task_id}/runs").status_code == 200

        duplicate = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "invalid_task_transition"
