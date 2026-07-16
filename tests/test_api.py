from dataclasses import replace

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.services import RunExecutionService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.features import FeatureGateSet
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


def test_minimal_profile_keeps_task_api_and_blocks_advanced_apis(
    application_container: ApplicationContainer,
) -> None:
    minimal_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )

    with TestClient(create_app(minimal_container)) as client:
        features = client.get("/api/v1/features")
        task = client.post("/api/v1/tasks", json={"objective": "Keep the core path simple"})
        agents = client.get("/api/v1/agents")

        assert features.status_code == 200
        assert features.json()["profile"] == "minimal"
        assert features.json()["restart_required"] is True
        assert all(not item["enabled"] for item in features.json()["features"])
        assert task.status_code == 201
        assert agents.status_code == 403
        assert agents.json()["code"] == "feature_disabled"


def test_standard_profile_enables_registry_but_not_deployments(
    application_container: ApplicationContainer,
) -> None:
    standard_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("standard"),
    )

    with TestClient(create_app(standard_container)) as client:
        assert client.get("/api/v1/agents").status_code == 200
        deployment = client.get(
            "/api/v1/agent-versions/00000000-0000-0000-0000-000000000000/deployments"
        )
        assert deployment.status_code == 403
        assert "agent_deployments" in deployment.json()["message"]


def test_agent_registry_api_lifecycle(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        capability = client.post(
            "/api/v1/capabilities",
            json={
                "key": "document.summarize",
                "version": "1.0.0",
                "description": "Summarize a document",
            },
        )
        assert capability.status_code == 201

        created = client.post(
            "/api/v1/agents",
            json={
                "owner_id": "docs-team",
                "name": "document-summarizer",
                "description": "Summarizes documents",
                "visibility": "TENANT",
                "tags": ["documents"],
            },
        )
        assert created.status_code == 201
        definition_id = created.json()["id"]
        duplicate = client.post(
            "/api/v1/agents",
            json={
                "owner_id": "another-team",
                "name": "document-summarizer",
                "description": "Duplicate",
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "agent_registry_conflict"

        draft = client.post(
            f"/api/v1/agents/{definition_id}/versions",
            json={
                "semantic_version": "1.0.0",
                "role": "Document summarizer",
                "instructions": "Return a concise structured summary.",
                "declared_capabilities": ["document.summarize"],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        )
        assert draft.status_code == 201
        version_id = draft.json()["id"]
        assert draft.json()["status"] == "DRAFT"

        submitted = client.post(f"/api/v1/agent-versions/{version_id}/submit-review")
        assert submitted.json()["status"] == "IN_REVIEW"
        published = client.post(
            f"/api/v1/agent-versions/{version_id}/publish",
            json={
                "verified_capabilities": ["document.summarize"],
                "make_default": True,
            },
        )
        assert published.status_code == 200
        assert published.json()["status"] == "PUBLISHED"
        assert published.json()["content_digest"].startswith("sha256:")

        fetched = client.get(f"/api/v1/agents/{definition_id}")
        assert fetched.json()["default_version_id"] == version_id

        candidates = client.post(
            "/api/v1/agent-candidates:search",
            json={"required_capabilities": ["document.summarize"]},
        )
        assert candidates.status_code == 200
        assert candidates.json()[0]["agent_version"]["id"] == version_id
