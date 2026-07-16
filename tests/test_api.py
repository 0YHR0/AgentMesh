import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.services import RunExecutionService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.tools import (
    ToolBinding,
    ToolCallResult,
    ToolSideEffect,
    canonical_json_digest,
)
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


def test_task_api_pauses_and_resumes_durable_run(
    application_container: ApplicationContainer,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        task_id = client.post(
            "/api/v1/tasks",
            json={"objective": "Pause through the API"},
        ).json()["id"]
        queued = client.post(f"/api/v1/tasks/{task_id}/runs")
        original_wakeup = uow_factory.store.outbox[0]

        paused = client.post(f"/api/v1/tasks/{task_id}/pause")
        assert paused.status_code == 202
        assert paused.headers["location"] == f"/api/v1/tasks/{task_id}"
        assert paused.json()["status"] == "PAUSED"
        assert paused.json()["runs"][0]["status"] == "PAUSED"
        assert paused.json()["runs"][0]["paused_at"] is not None
        assert execution_service.process(original_wakeup) is False

        resumed = client.post(f"/api/v1/tasks/{task_id}/resume")
        assert resumed.status_code == 202
        assert resumed.headers["location"] == f"/api/v1/tasks/{task_id}"
        assert resumed.json()["status"] == "READY"
        assert resumed.json()["runs"][0]["status"] == "QUEUED"
        assert resumed.json()["runs"][0]["resumed_at"] is not None

        resume_wakeup = next(
            item
            for item in reversed(uow_factory.store.outbox)
            if item.schema_name == original_wakeup.schema_name
            and item.message_id != original_wakeup.message_id
        )
        assert execution_service.process(resume_wakeup) is True
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "COMPLETED"
        assert queued.status_code == 202


def test_task_api_rejects_pause_before_run(application_container: ApplicationContainer) -> None:
    with TestClient(create_app(application_container)) as client:
        task_id = client.post(
            "/api/v1/tasks",
            json={"objective": "No active run"},
        ).json()["id"]

        response = client.post(f"/api/v1/tasks/{task_id}/pause")

        assert response.status_code == 409
        assert response.json()["code"] == "invalid_task_transition"


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


def test_minimal_profile_rejects_mcp_tool_requests_at_the_api_boundary(
    application_container: ApplicationContainer,
) -> None:
    minimal_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(minimal_container)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Do not expose optional tools",
                "input": {
                    "tool_call": {
                        "tool": "workspace.read_text",
                        "arguments": {"path": "README.md"},
                    }
                },
            },
        )

        assert response.status_code == 403
        assert response.json()["code"] == "feature_disabled"
        assert "mcp_read_tools" in response.json()["message"]


def test_full_profile_rejects_an_mcp_tool_outside_the_allowlist(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Reject an unknown Tool",
                "input": {
                    "tool_call": {
                        "tool": "filesystem.delete",
                        "arguments": {"path": "README.md"},
                    }
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_tool_request"
    assert "not in the current allowlist" in response.json()["message"]


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


def test_mcp_invocation_audit_api_returns_digests_not_payloads(
    application_container: ApplicationContainer,
    task_service,
    tool_invocation_service: ToolInvocationService,
) -> None:
    task = task_service.create_task(
        "Read a file",
        {
            "tool_call": {
                "tool": "workspace.read_text",
                "arguments": {"path": "README.md"},
            }
        },
    )
    run = task_service.request_run(task.task.id).runs[0]
    binding = ToolBinding(
        logical_key="workspace.read_text",
        server_name="agentmesh-workspace",
        tool_name="read_text",
        side_effect=ToolSideEffect.READ_ONLY,
    )
    invocation = tool_invocation_service.start(
        task_id=task.task.id,
        run_id=run.id,
        binding=binding,
        arguments={"path": "README.md"},
    )
    output = {"structured_content": {"path": "README.md"}}
    tool_invocation_service.succeed(
        invocation.id,
        ToolCallResult(
            output=output,
            protocol_version="2025-11-25",
            schema_digest=canonical_json_digest({"type": "object"}),
            result_digest=canonical_json_digest(output),
            result_bytes=48,
        ),
    )

    with TestClient(create_app(application_container)) as client:
        response = client.get(f"/api/v1/tasks/{task.task.id}/tool-invocations")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "SUCCEEDED"
    assert item["side_effect"] == "READ_ONLY"
    assert item["arguments_digest"].startswith("sha256:")
    assert "arguments" not in item
    assert "result" not in item


def test_mcp_audit_api_is_disabled_outside_full_profile(
    application_container: ApplicationContainer,
) -> None:
    standard_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("standard"),
    )
    with TestClient(create_app(standard_container)) as client:
        response = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000/tool-invocations")

    assert response.status_code == 403
    assert "mcp_read_tools" in response.json()["message"]


def test_artifact_api_creates_versions_and_downloads_verified_content(
    application_container: ApplicationContainer,
) -> None:
    first_content = b'{"summary":"first"}'
    second_content = b'{"summary":"second"}'
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/artifacts",
            headers={"Idempotency-Key": "artifact-api-1"},
            json={
                "display_name": "summary.json",
                "kind": "task.result",
                "classification": "INTERNAL",
                "media_type": "application/json",
                "content_base64": base64.b64encode(first_content).decode(),
            },
        )
        replay = client.post(
            "/api/v1/artifacts",
            headers={"Idempotency-Key": "artifact-api-1"},
            json={
                "display_name": "summary.json",
                "kind": "task.result",
                "classification": "INTERNAL",
                "media_type": "application/json",
                "content_base64": base64.b64encode(first_content).decode(),
            },
        )

        assert created.status_code == 201
        assert replay.json()["id"] == created.json()["id"]
        artifact_id = created.json()["id"]
        first_version_id = created.json()["versions"][0]["id"]
        updated = client.post(
            f"/api/v1/artifacts/{artifact_id}/versions",
            json={
                "media_type": "application/json",
                "content_base64": base64.b64encode(second_content).decode(),
            },
        )
        download = client.get(f"/api/v1/artifact-versions/{first_version_id}/content")

        assert updated.status_code == 201
        assert updated.json()["version_count"] == 2
        assert [item["version_number"] for item in updated.json()["versions"]] == [1, 2]
        assert download.status_code == 200
        assert download.content == first_content
        assert download.headers["content-type"] == "application/json"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["digest"].startswith("sha-256=")


def test_artifact_api_is_disabled_outside_full_profile(
    application_container: ApplicationContainer,
) -> None:
    standard_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("standard"),
    )
    with TestClient(create_app(standard_container)) as client:
        response = client.get("/api/v1/artifacts")

        assert response.status_code == 403
        assert response.json()["code"] == "feature_disabled"
        assert "artifact_service" in response.json()["message"]


def test_artifact_api_rejects_invalid_base64(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/artifacts",
            json={
                "display_name": "invalid.txt",
                "kind": "document.text",
                "classification": "INTERNAL",
                "media_type": "text/plain",
                "content_base64": "not-base64!",
            },
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_artifact"


def test_artifact_api_rejects_content_over_configured_limit(
    application_container: ApplicationContainer,
) -> None:
    oversized = base64.b64encode(b"x" * 65_537).decode()
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/artifacts",
            json={
                "display_name": "large.txt",
                "kind": "document.text",
                "classification": "INTERNAL",
                "media_type": "text/plain",
                "content_base64": oversized,
            },
        )

        assert response.status_code == 413
        assert response.json()["code"] == "artifact_too_large"


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
