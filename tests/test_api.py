from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.services import RunExecutionService
from agentmesh.bootstrap import ApplicationContainer
from tests.fakes import InMemoryUnitOfWorkFactory


def test_task_api_accepts_then_worker_completes(
    application_container: ApplicationContainer,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        assert client.get("/health").status_code == 200

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
        assert created.json()["tenant_id"] == "test-tenant"

        accepted = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert accepted.status_code == 202
        assert accepted.headers["location"] == f"/api/v1/tasks/{task_id}"
        assert accepted.json()["status"] == "READY"
        assert accepted.json()["runs"][0]["status"] == "QUEUED"

        execution_service.process(uow_factory.store.outbox[0])
        fetched = client.get(f"/api/v1/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "COMPLETED"
        assert fetched.json()["output"]["input"] == {"source": "api-test"}
        assert fetched.json()["runs"][0]["status"] == "SUCCEEDED"
        assert fetched.json()["attempts"][0]["status"] == "SUCCEEDED"


def test_duplicate_run_returns_conflict(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post("/api/v1/tasks", json={"objective": "Run once"})
        task_id = created.json()["id"]
        assert client.post(f"/api/v1/tasks/{task_id}/runs").status_code == 202

        duplicate = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "invalid_task_transition"


def test_idempotency_header_replays_response(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        task_id = client.post("/api/v1/tasks", json={"objective": "Replay safely"}).json()["id"]
        headers = {"Idempotency-Key": "api-request-1"}

        first = client.post(f"/api/v1/tasks/{task_id}/runs", headers=headers)
        replay = client.post(f"/api/v1/tasks/{task_id}/runs", headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["current_run_id"] == first.json()["current_run_id"]


def test_blank_idempotency_header_is_rejected(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        task_id = client.post("/api/v1/tasks", json={"objective": "Reject ambiguous key"}).json()[
            "id"
        ]

        response = client.post(
            f"/api/v1/tasks/{task_id}/runs",
            headers={"Idempotency-Key": "   "},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_task_input"
