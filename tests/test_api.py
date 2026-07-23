import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.registry_services import AgentRegistryService
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


def test_web_console_is_served_with_its_zero_build_assets(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in index.headers["content-security-policy"]
        assert index.headers["x-content-type-options"] == "nosniff"
        assert "AgentMesh Console" in index.text
        assert 'id="create-form"' in index.text
        assert 'id="dag"' in index.text
        assert 'id="agents-nav"' in index.text
        assert 'id="agent-version-list"' in index.text
        assert 'id="tool-audit-list"' in index.text
        assert 'id="agent-form"' in index.text
        assert 'id="version-form"' in index.text
        assert 'id="publish-form"' in index.text
        assert 'id="approvals-nav"' in index.text
        assert 'id="approval-detail"' in index.text
        assert 'id="decision-form"' in index.text
        assert 'id="artifacts-nav"' in index.text
        assert 'id="artifact-detail"' in index.text
        assert 'id="artifact-form"' in index.text
        assert 'id="task-artifact-panel"' in index.text
        assert 'id="planning-panel"' in index.text
        assert 'id="plan-patch-form"' in index.text
        assert 'id="mission-view"' in index.text
        assert 'id="mission-canvas"' in index.text
        assert 'id="mission-inspector"' in index.text
        assert 'id="mission-view-button"' in index.text
        assert 'id="board-view-button"' in index.text
        assert 'id="mission-replay-range"' in index.text
        assert 'id="mission-replay-bookmarks"' in index.text
        assert 'id="mission-replay-export"' in index.text

        script = client.get("/console/assets/app.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert 'api("/api/v1/tasks' in script.text
        assert 'api("/api/v1/agents' in script.text
        assert 'tool-invocations' in script.text
        assert 'featureEnabled("agent_registry_management")' in script.text
        assert 'Execution-Permit-Id' in script.text
        assert 'submit-review' in script.text
        assert 'model_policy: modelPolicy' in script.text
        assert 'api("/api/v1/approvals?limit=100&offset=0")' in script.text
        assert 'action_type: "agent.version.publish"' in script.text
        assert 'navigator.clipboard.writeText' in script.text
        assert 'api("/api/v1/artifacts?limit=100&offset=0")' in script.text
        assert 'data-preview-version' in script.text
        assert 'producer_run_id' in script.text
        assert 'fetch("/api/v1/events"' in script.text
        assert '"Last-Event-ID": state.streamCursor' in script.text
        assert 'featureEnabled("realtime_events") ? 15000 : 3000' in script.text
        assert 'api(`/api/v1/tasks/${id}/activity?limit=100`)' in script.text
        assert 'api(`/api/v1/tasks/${id}/interactions?limit=100`)' in script.text
        assert 'api(`/api/v1/tasks/${id}/planning`)' in script.text
        assert '/plan-patches/${patchId}/apply' in script.text
        assert "finding.code" in script.text
        assert "function renderMissionMap" in script.text
        assert "function deriveMissionPulses" in script.text
        assert "function missionInteractionRoutes" in script.text
        assert "function missionVisibleInteractions" in script.text
        assert "function missionReplayEvents" in script.text
        assert "function missionReplayTask" in script.text
        assert "function toggleMissionReplay" in script.text
        assert '"agentmesh.mission-replay.v1"' in script.text
        assert 'localStorage.getItem("agentmesh-mission-bookmarks")' in script.text
        assert 'sessionStorage.getItem("agentmesh-mission-filter")' in script.text
        assert "GOVERNED INTERACTION DOCK" in script.text
        assert 'id="mission-filters"' in index.text
        assert 'id="mission-filter-trace"' in index.text
        assert "<animateMotion" in script.text
        assert 'id="activity-panel"' in index.text

        stylesheet = client.get("/console/assets/app.css")
        assert stylesheet.status_code == 200
        assert ".version-card" in stylesheet.text
        assert ".audit-item" in stylesheet.text
        assert ".plan-patch-card" in stylesheet.text
        assert ".mission-station" in stylesheet.text
        assert ".mission-route" in stylesheet.text
        assert ".interaction-route" in stylesheet.text
        assert ".mission-external" in stylesheet.text
        assert ".mission-filters" in stylesheet.text
        assert ".mission-replay" in stylesheet.text


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


def test_task_api_exposes_review_contract_and_run_roles(
    application_container: ApplicationContainer,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Return a reviewed summary",
                "execution_mode": "REVIEWED",
                "acceptance_criteria": [
                    {
                        "key": "summary",
                        "description": "A summary is present",
                        "kind": "OUTPUT_PATH_EXISTS",
                        "path": ["summary"],
                    }
                ],
                "max_revisions": 1,
            },
        )
        assert created.status_code == 201
        assert created.json()["execution_mode"] == "REVIEWED"
        task_id = created.json()["id"]
        client.post(f"/api/v1/tasks/{task_id}/runs")

        assert execution_service.process(uow_factory.store.outbox[0]) is True
        review_wakeup = next(
            item
            for item in reversed(uow_factory.store.outbox)
            if item != uow_factory.store.outbox[0]
            and item.schema_name == uow_factory.store.outbox[0].schema_name
        )
        assert execution_service.process(review_wakeup) is True

        fetched = client.get(f"/api/v1/tasks/{task_id}").json()
        assert fetched["status"] == "COMPLETED"
        assert [run["role"] for run in fetched["runs"]] == ["EXECUTOR", "REVIEWER"]
        assert fetched["latest_review"]["score_basis_points"] == 10_000


def test_task_api_runs_coordinated_dag_and_exposes_subtasks(
    application_container: ApplicationContainer,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Coordinate API work",
                "execution_mode": "COORDINATED",
                "max_concurrency": 2,
                "subtasks": [
                    {"key": "left", "objective": "Produce left"},
                    {"key": "right", "objective": "Produce right"},
                    {
                        "key": "join",
                        "objective": "Join results",
                        "depends_on": ["left", "right"],
                    },
                ],
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["plan_digest"].startswith("sha256:")
        started = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert started.status_code == 202
        assert len(started.json()["runs"]) == 2

        for run in started.json()["runs"]:
            wakeup = next(
                item for item in uow_factory.store.outbox if item.payload.get("run_id") == run["id"]
            )
            assert execution_service.process(wakeup) is True

        joined = client.get(f"/api/v1/tasks/{task_id}").json()
        join = next(subtask for subtask in joined["subtasks"] if subtask["key"] == "join")
        assert join["depends_on"] == ["left", "right"]
        join_run = next(run for run in joined["runs"] if run["subtask_id"] == join["id"])
        join_wakeup = next(
            item
            for item in uow_factory.store.outbox
            if item.payload.get("run_id") == join_run["id"]
        )
        assert execution_service.process(join_wakeup) is True

        supervisor_state = client.get(f"/api/v1/tasks/{task_id}").json()
        supervisor = next(run for run in supervisor_state["runs"] if run["role"] == "SUPERVISOR")
        supervisor_wakeup = next(
            item
            for item in uow_factory.store.outbox
            if item.payload.get("run_id") == supervisor["id"]
        )
        assert execution_service.process(supervisor_wakeup) is True
        completed = client.get(f"/api/v1/tasks/{task_id}").json()
        assert completed["status"] == "COMPLETED"
        assert len(completed["attempts"]) == 4


def test_planning_api_verifies_and_applies_pre_execution_patch(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Produce a governed brief",
                "execution_mode": "COORDINATED",
                "goal": {
                    "constraints": ["Use traceable evidence"],
                    "success_criteria": ["Return one recommendation"],
                },
                "subtasks": [
                    {"key": "research", "objective": "Research"},
                    {
                        "key": "synthesize",
                        "objective": "Synthesize",
                        "depends_on": ["research"],
                    },
                ],
            },
        )
        assert created.status_code == 201
        task = created.json()
        task_id = task["id"]

        snapshot = client.get(f"/api/v1/tasks/{task_id}/planning")
        assert snapshot.status_code == 200
        assert snapshot.json()["goal"]["constraints"] == ["Use traceable evidence"]

        proposed = client.post(
            f"/api/v1/tasks/{task_id}/plan-patches",
            json={
                "base_plan_version": task["plan_version"],
                "base_plan_digest": task["plan_digest"],
                "reason": "Add analysis before synthesis",
                "requested_by": "api-operator",
                "max_concurrency": 2,
                "subtasks": [
                    {"key": "research", "objective": "Research"},
                    {
                        "key": "analyze",
                        "objective": "Analyze",
                        "depends_on": ["research"],
                    },
                    {
                        "key": "synthesize",
                        "objective": "Synthesize",
                        "depends_on": ["analyze"],
                    },
                ],
            },
        )
        assert proposed.status_code == 201
        patch = proposed.json()
        assert patch["status"] == "VERIFIED"
        assert all(item["passed"] for item in patch["evidence"])

        applied = client.post(
            f"/api/v1/tasks/{task_id}/plan-patches/{patch['id']}/apply"
        )
        assert applied.status_code == 200
        assert applied.json()["status"] == "APPLIED"
        updated = client.get(f"/api/v1/tasks/{task_id}").json()
        assert updated["plan_version"] == 2
        assert [item["key"] for item in updated["subtasks"]] == [
            "analyze",
            "research",
            "synthesize",
        ]


def test_handoff_api_exposes_request_and_acceptance(
    application_container: ApplicationContainer,
    execution_service: RunExecutionService,
    registry_service: AgentRegistryService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Coordinate an API Handoff",
                "execution_mode": "COORDINATED",
                "max_concurrency": 2,
                "subtasks": [
                    {"key": "source", "objective": "Produce source"},
                    {"key": "other", "objective": "Produce other"},
                    {
                        "key": "target",
                        "objective": "Synthesize",
                        "depends_on": ["source", "other"],
                    },
                ],
            },
        ).json()
        task_id = created["id"]
        started = client.post(f"/api/v1/tasks/{task_id}/runs").json()
        source = next(value for value in started["subtasks"] if value["key"] == "source")
        target = next(value for value in started["subtasks"] if value["key"] == "target")
        source_run = next(value for value in started["runs"] if value["subtask_id"] == source["id"])
        source_wakeup = next(
            item
            for item in uow_factory.store.outbox
            if item.payload.get("run_id") == source_run["id"]
        )
        assert execution_service.process(source_wakeup) is True
        registry_service.ensure_builtin_agent("z-handoff-agent")

        requested = client.post(
            f"/api/v1/tasks/{task_id}/handoffs",
            headers={"Idempotency-Key": "api-handoff-request"},
            json={
                "source_subtask_id": source["id"],
                "target_subtask_id": target["id"],
                "target_agent_id": "z-handoff-agent",
                "objective": "Own synthesis",
                "reason": "Specialist routing",
                "completed_work_summary": "Source is complete",
                "requested_by": source_run["agent_id"],
            },
        )
        assert requested.status_code == 201
        assert requested.json()["status"] == "REQUESTED"
        handoff_id = requested.json()["id"]

        accepted = client.post(
            f"/api/v1/tasks/{task_id}/handoffs/{handoff_id}/accept",
            headers={"Idempotency-Key": "api-handoff-accept"},
            json={"actor": "z-handoff-agent", "reason": "Accepted"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "ACCEPTED"
        fetched = client.get(f"/api/v1/tasks/{task_id}").json()
        assert fetched["handoffs"][0]["id"] == handoff_id
        assert fetched["handoffs"][0]["decided_by"] == "z-handoff-agent"


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
        credentials = client.get("/api/v1/credentials/secret-references")

        assert features.status_code == 200
        assert features.json()["profile"] == "minimal"
        assert features.json()["restart_required"] is True
        assert all(not item["enabled"] for item in features.json()["features"])
        assert task.status_code == 201
        assert agents.status_code == 403
        assert agents.json()["code"] == "feature_disabled"
        assert credentials.status_code == 403
        assert credentials.json()["code"] == "feature_disabled"


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
