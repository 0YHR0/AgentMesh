from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text

from agentmesh.api.app import create_app
from agentmesh.bootstrap import (
    build_api_container,
    build_relay_container,
    build_worker_container,
    seed_builtin_registry,
)
from agentmesh.config import get_settings

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
        finally:
            engine.dispose()

        assert checkpoint_count > 0
        assert outbox_status == "PUBLISHED"
        assert inbox_count == 1
        assert bound_version_status == "PUBLISHED"
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
