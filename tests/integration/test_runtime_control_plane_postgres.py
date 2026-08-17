"""PostgreSQL evidence for the A1 runtime registry persistence boundary.

These tests intentionally use the migrated PostgreSQL service from CI.  The
domain tests cover the transition matrix; this module verifies the database
constraints and idempotent bootstrap against the real engine.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_runtime_schema_has_a1_constraints_and_indexes() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        database = inspect(engine)
        checks = {
            constraint["name"]
            for table in (
                "runtime_observations",
                "runtime_lifecycle_operations",
            )
            for constraint in database.get_check_constraints(table)
        }
        assert {
            "ck_runtime_observations_digests",
            "ck_runtime_observations_phase",
            "ck_runtime_lifecycle_digest",
        } <= checks
        indexes = {
            index["name"]
            for index in database.get_indexes("task_runs")
        }
        assert {
            "ix_task_runs_runtime_execution",
            "ix_task_runs_runtime_version",
        } <= indexes
    finally:
        engine.dispose()


def test_builtin_runtime_seed_is_deterministic_and_idempotent() -> None:
    settings = get_settings()
    seed_builtin_registry(settings)
    seed_builtin_registry(settings)
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, api_version, descriptor->>'runtime_key' AS runtime_key "
                    "FROM runtime_versions "
                    "WHERE runtime_id = 'c5b0d15a-33ef-57dc-92d6-c685bcd31470'"
                )
            ).all()
        assert len(rows) == 1
        assert str(rows[0].id) == "5c6ffbe8-2226-5fbc-bddf-08388949e82e"
        assert rows[0].api_version == 1
        assert rows[0].runtime_key == "agentmesh.langgraph"
    finally:
        engine.dispose()
