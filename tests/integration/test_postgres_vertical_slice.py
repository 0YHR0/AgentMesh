from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from agentmesh.api.app import create_app
from agentmesh.config import get_settings

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
    ),
]


def test_real_postgres_task_and_checkpoint_flow() -> None:
    with TestClient(create_app()) as client:
        readiness = client.get("/ready")
        assert readiness.status_code == 200

        created = client.post(
            "/api/v1/tasks",
            json={
                "objective": "Verify the real PostgreSQL vertical slice",
                "input": {"source": "pytest-integration"},
            },
        )
        assert created.status_code == 201

        task_id = created.json()["id"]
        completed = client.post(f"/api/v1/tasks/{task_id}/runs")
        assert completed.status_code == 200

        payload = completed.json()
        assert payload["status"] == "COMPLETED"
        assert payload["runs"][0]["status"] == "COMPLETED"
        assert payload["output"]["input"] == {"source": "pytest-integration"}
        thread_id = payload["runs"][0]["thread_id"]

    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            checkpoint_count = connection.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            ).scalar_one()
    finally:
        engine.dispose()

    assert checkpoint_count > 0
