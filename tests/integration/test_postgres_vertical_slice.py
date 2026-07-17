from __future__ import annotations

import base64
import json
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.api.app import create_app
from agentmesh.bootstrap import (
    build_api_container,
    build_relay_container,
    build_worker_container,
    seed_builtin_registry,
)
from agentmesh.config import get_settings
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.observability import UsageRecord
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_real_postgres_redis_and_checkpoint_flow() -> None:
    suffix = uuid4().hex
    settings = get_settings().model_copy(
        update={
            "tenant_id": f"integration-{suffix}",
            "execution_stream": f"agentmesh.test.runs.{suffix}",
            "domain_event_stream": f"agentmesh.test.events.{suffix}",
            "execution_group": f"agentmesh-test-workers-{suffix}",
            "execution_consumer_name": f"test-run-executor-{suffix}",
            "dead_letter_stream": f"agentmesh.test.dead.{suffix}",
            "worker_block_ms": 100,
            "feature_profile": "full",
        }
    )
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    seed_builtin_registry(settings)
    api_container = build_api_container(settings)
    relay_container = build_relay_container(settings, relay_id=f"relay-{suffix}")
    worker_container = build_worker_container(settings, worker_id=f"worker-{suffix}")

    try:
        with TestClient(create_app(api_container)) as client:
            assert client.get("/ready").status_code == 200
            agents = client.get("/api/v1/agents").json()["items"]
            builtin = next(item for item in agents if item["name"] == settings.agent_id)
            assert builtin["versions"][0]["status"] == "PUBLISHED"
            created = client.post(
                "/api/v1/tasks",
                json={
                    "objective": "Verify the durable asynchronous vertical slice",
                    "input": {"source": "pytest-integration"},
                },
            )
            assert created.status_code == 201

            task_id = created.json()["id"]
            accepted = client.post(
                f"/api/v1/tasks/{task_id}/runs",
                headers={"Idempotency-Key": f"integration-{suffix}"},
            )
            assert accepted.status_code == 202
            assert accepted.json()["status"] == "READY"

            assert relay_container.relay.publish_once() >= 3
            assert redis_client.xlen(settings.domain_event_stream) >= 2
            assert worker_container.worker.run_once() == 1

            payload = client.get(f"/api/v1/tasks/{task_id}").json()
            assert payload["status"] == "COMPLETED"
            assert payload["runs"][0]["status"] == "SUCCEEDED"
            assert payload["runs"][0]["agent_version_id"] is not None
            assert payload["runs"][0]["agent_version_digest"].startswith("sha256:")
            assert payload["attempts"][0]["status"] == "SUCCEEDED"
            assert payload["output"]["input"] == {"source": "pytest-integration"}
            thread_id = payload["runs"][0]["thread_id"]
            run_id = payload["runs"][0]["id"]
            attempt = payload["attempts"][0]
            assert attempt["trace_id"] == UUID(attempt["id"]).hex

            reviewed = client.post(
                "/api/v1/tasks",
                json={
                    "objective": "Verify independent reviewed execution",
                    "execution_mode": "REVIEWED",
                    "acceptance_criteria": [
                        {
                            "key": "summary",
                            "description": "The candidate contains a summary",
                            "kind": "OUTPUT_PATH_EXISTS",
                            "path": ["summary"],
                        }
                    ],
                    "max_revisions": 1,
                },
            )
            assert reviewed.status_code == 201
            reviewed_task_id = reviewed.json()["id"]
            assert client.post(f"/api/v1/tasks/{reviewed_task_id}/runs").status_code == 202
            assert relay_container.relay.publish_once() >= 1
            assert worker_container.worker.run_once() == 1
            reviewing = client.get(f"/api/v1/tasks/{reviewed_task_id}").json()
            assert reviewing["status"] == "REVIEWING"
            assert [run["role"] for run in reviewing["runs"]] == ["EXECUTOR", "REVIEWER"]
            assert relay_container.relay.publish_once() >= 1
            assert worker_container.worker.run_once() == 1
            reviewed_result = client.get(f"/api/v1/tasks/{reviewed_task_id}").json()
            assert reviewed_result["status"] == "COMPLETED"
            assert reviewed_result["latest_review"]["score_basis_points"] == 10_000

            retention_report = relay_container.retention.run_if_due()
            assert retention_report is not None
            envelope_engine = create_engine(settings.database_url)
            try:
                with envelope_engine.connect() as connection:
                    duplicate_envelope = connection.execute(
                        text(
                            "SELECT envelope FROM outbox_events "
                            "WHERE tenant_id = :tenant_id AND topic = :topic "
                            "AND envelope -> 'payload' ->> 'run_id' = :run_id"
                        ),
                        {
                            "tenant_id": settings.tenant_id,
                            "topic": RUN_REQUESTED_SCHEMA,
                            "run_id": run_id,
                        },
                    ).scalar_one()
            finally:
                envelope_engine.dispose()
            redis_client.xadd(
                settings.execution_stream,
                {
                    "envelope": json.dumps(
                        duplicate_envelope,
                        separators=(",", ":"),
                    )
                },
            )
            assert worker_container.worker.run_once() == 1
            duplicate_result = client.get(f"/api/v1/tasks/{task_id}").json()
            assert len(duplicate_result["attempts"]) == 1

            usage_engine = create_engine(settings.database_url)
            usage_uow_factory = SqlAlchemyUnitOfWorkFactory(
                sessionmaker(
                    bind=usage_engine,
                    expire_on_commit=False,
                    class_=Session,
                )
            )
            usage_record = UsageRecord.create(
                tenant_id=settings.tenant_id,
                task_id=UUID(task_id),
                run_id=UUID(run_id),
                attempt_id=UUID(attempt["id"]),
                trace_id=attempt["trace_id"],
                provider="integration-provider",
                model="integration-model",
                usage_details={"input": 8, "output": 2, "total": 10},
                cost_details_micros={"total": 25},
                pricing_version="integration-v1",
            )
            try:
                with usage_uow_factory() as uow:
                    assert uow.usage_records.add_if_absent(usage_record) is True
                    assert uow.usage_records.add_if_absent(usage_record) is False
                    uow.commit()
            finally:
                usage_engine.dispose()

            usage_payload = client.get(f"/api/v1/tasks/{task_id}/usage")
            assert usage_payload.status_code == 200
            assert usage_payload.json()["usage_details"]["total"] == 10
            assert usage_payload.json()["cost_details_micros_by_currency"]["USD"]["total"] == 25

            pause_task = client.post(
                "/api/v1/tasks",
                json={"objective": "Verify durable pause and resume"},
            )
            pause_task_id = pause_task.json()["id"]
            pause_run = client.post(f"/api/v1/tasks/{pause_task_id}/runs")
            assert pause_run.status_code == 202
            paused = client.post(f"/api/v1/tasks/{pause_task_id}/pause")
            assert paused.status_code == 202
            assert paused.json()["status"] == "PAUSED"
            assert relay_container.relay.publish_once() >= 2
            assert worker_container.worker.run_once() == 1
            persisted_pause = client.get(f"/api/v1/tasks/{pause_task_id}").json()
            assert persisted_pause["status"] == "PAUSED"
            assert persisted_pause["attempts"] == []

            resumed = client.post(f"/api/v1/tasks/{pause_task_id}/resume")
            assert resumed.status_code == 202
            assert resumed.json()["status"] == "READY"
            assert relay_container.relay.publish_once() >= 2
            assert worker_container.worker.run_once() == 1
            resumed_payload = client.get(f"/api/v1/tasks/{pause_task_id}").json()
            assert resumed_payload["status"] == "COMPLETED"
            assert resumed_payload["runs"][0]["paused_at"] is not None
            assert resumed_payload["runs"][0]["resumed_at"] is not None
            paused_thread_id = resumed_payload["runs"][0]["thread_id"]

            mcp_task = client.post(
                "/api/v1/tasks",
                json={
                    "objective": "Read AgentMesh documentation through MCP",
                    "input": {
                        "tool_call": {
                            "tool": "workspace.read_text",
                            "arguments": {"path": "README.md"},
                        }
                    },
                },
            )
            assert mcp_task.status_code == 201
            mcp_task_id = mcp_task.json()["id"]
            assert client.post(f"/api/v1/tasks/{mcp_task_id}/runs").status_code == 202
            assert relay_container.relay.publish_once() >= 1
            assert worker_container.worker.run_once() == 1
            mcp_payload = client.get(f"/api/v1/tasks/{mcp_task_id}").json()
            assert mcp_payload["status"] == "COMPLETED"
            assert (
                mcp_payload["output"]["tool_invocation"]["result"]["structured_content"]["path"]
                == "README.md"
            )
            mcp_audit = client.get(f"/api/v1/tasks/{mcp_task_id}/tool-invocations").json()["items"]
            assert len(mcp_audit) == 1
            assert mcp_audit[0]["status"] == "SUCCEEDED"
            assert mcp_audit[0]["protocol_version"] == "2025-11-25"

            artifact_content = b'{"verified":"postgres"}'
            artifact = client.post(
                "/api/v1/artifacts",
                headers={"Idempotency-Key": f"artifact-{suffix}"},
                json={
                    "display_name": "integration-result.json",
                    "kind": "task.result",
                    "classification": "INTERNAL",
                    "media_type": "application/json",
                    "content_base64": base64.b64encode(artifact_content).decode(),
                    "producer_run_id": run_id,
                },
            )
            assert artifact.status_code == 201
            artifact_version_id = artifact.json()["versions"][0]["id"]
            downloaded = client.get(f"/api/v1/artifact-versions/{artifact_version_id}/content")
            assert downloaded.content == artifact_content
            assert relay_container.relay.publish_once() >= 2

        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                checkpoint_count = connection.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": thread_id},
                ).scalar_one()
                outbox_status = connection.execute(
                    text(
                        "SELECT status FROM outbox_events "
                        "WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"tenant_id": settings.tenant_id},
                ).scalar_one()
                inbox_count = connection.execute(
                    text(
                        "SELECT count(*) FROM inbox_messages WHERE consumer_name = :consumer_name"
                    ),
                    {"consumer_name": settings.execution_consumer_name},
                ).scalar_one()
                bound_version_status = connection.execute(
                    text(
                        "SELECT av.status FROM task_runs tr "
                        "JOIN agent_versions av ON av.id = tr.agent_version_id "
                        "WHERE tr.thread_id = :thread_id"
                    ),
                    {"thread_id": thread_id},
                ).scalar_one()
                artifact_version_count = connection.execute(
                    text("SELECT count(*) FROM artifact_versions WHERE producer_run_id = :run_id"),
                    {"run_id": run_id},
                ).scalar_one()
                pause_timestamp_count = connection.execute(
                    text(
                        "SELECT count(*) FROM task_runs "
                        "WHERE thread_id = :thread_id "
                        "AND paused_at IS NOT NULL AND resumed_at IS NOT NULL"
                    ),
                    {"thread_id": paused_thread_id},
                ).scalar_one()
                tool_invocation_count = connection.execute(
                    text(
                        "SELECT count(*) FROM tool_invocations "
                        "WHERE task_id = :task_id AND status = 'SUCCEEDED'"
                    ),
                    {"task_id": mcp_task_id},
                ).scalar_one()
                usage_record_count = connection.execute(
                    text("SELECT count(*) FROM usage_records WHERE task_id = :task_id"),
                    {"task_id": task_id},
                ).scalar_one()
        finally:
            engine.dispose()

        assert checkpoint_count > 0
        assert outbox_status == "PUBLISHED"
        assert inbox_count == 6
        assert bound_version_status == "PUBLISHED"
        assert artifact_version_count == 1
        assert pause_timestamp_count == 1
        assert tool_invocation_count == 1
        assert usage_record_count == 1
    finally:
        worker_container.close()
        relay_container.close()
        api_container.close()
        redis_client.delete(
            settings.execution_stream,
            settings.domain_event_stream,
            settings.dead_letter_stream,
        )
        redis_client.close()
