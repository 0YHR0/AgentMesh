from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("AGENTMESH_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
METRICS_URL = os.getenv(
    "AGENTMESH_E2E_METRICS_URL",
    f"http://127.0.0.1:{os.getenv('AGENTMESH_RELAY_METRICS_PORT', '9464')}/metrics",
)


def request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed local test origin
        return json.load(response)


def wait_until_ready(timeout_seconds: int = 90) -> None:
    print("Waiting for the API readiness probe", flush=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if request_json("/ready") == {"status": "ready"}:
                return
        except (HTTPError, URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise TimeoutError("AgentMesh did not become ready before the timeout")


def wait_for_relay_metrics(timeout_seconds: int = 90) -> None:
    print("Waiting for the Relay metrics endpoint", flush=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(METRICS_URL, timeout=10) as response:  # noqa: S310 - fixed local origin
                payload = response.read().decode("utf-8")
            values = [
                line.rsplit(" ", maxsplit=1)[-1]
                for line in payload.splitlines()
                if line.startswith(
                    "agentmesh_messaging_retention_last_success_timestamp_seconds "
                )
            ]
            if values and float(values[0]) > 0:
                return
        except (
            HTTPError,
            URLError,
            ConnectionError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
        ):
            pass
        time.sleep(1)
    raise TimeoutError("Relay metrics did not become available before the timeout")


def wait_for_task(task_id: str, timeout_seconds: int = 120) -> dict[str, Any]:
    print(f"Waiting for Task {task_id} to complete", flush=True)
    deadline = time.monotonic() + timeout_seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = request_json(f"/api/v1/tasks/{task_id}")
        status = payload.get("status")
        if status == "COMPLETED":
            return payload
        if status in {"FAILED", "CANCELED"}:
            raise AssertionError(f"Task reached terminal status {status}: {payload}")
        time.sleep(1)
    raise TimeoutError(f"Task did not complete before the timeout: {payload}")


def main() -> None:
    wait_until_ready()
    wait_for_relay_metrics()
    created = request_json(
        "/api/v1/tasks",
        method="POST",
        payload={
            "objective": "Verify the AgentMesh Compose path",
            "input": {"source": "compose-e2e"},
        },
    )
    task_id = str(created["id"])
    request_json(
        f"/api/v1/tasks/{task_id}/runs",
        method="POST",
        headers={"Idempotency-Key": f"compose-e2e-{os.getenv('GITHUB_RUN_ID', 'local')}"},
    )

    task = wait_for_task(task_id)
    assert task["runs"][0]["status"] == "SUCCEEDED"
    assert task["attempts"][0]["status"] == "SUCCEEDED"
    assert re.fullmatch(r"[0-9a-f]{32}", task["attempts"][0]["trace_id"])
    assert task["output"]["input"]["source"] == "compose-e2e"

    usage = request_json(f"/api/v1/tasks/{task_id}/usage")
    assert usage == {
        "task_id": task_id,
        "usage_details": {},
        "cost_details_micros_by_currency": {},
        "records": [],
    }

    reviewed = request_json(
        "/api/v1/tasks",
        method="POST",
        payload={
            "objective": "Verify the AgentMesh reviewed Compose path",
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
    reviewed_task_id = str(reviewed["id"])
    request_json(
        f"/api/v1/tasks/{reviewed_task_id}/runs",
        method="POST",
        headers={
            "Idempotency-Key": f"compose-reviewed-{os.getenv('GITHUB_RUN_ID', 'local')}"
        },
    )
    reviewed_task = wait_for_task(reviewed_task_id)
    assert [run["role"] for run in reviewed_task["runs"]] == ["EXECUTOR", "REVIEWER"]
    assert reviewed_task["latest_review"]["accepted"] is True
    assert reviewed_task["latest_review"]["score_basis_points"] == 10_000

    coordinated = request_json(
        "/api/v1/tasks",
        method="POST",
        payload={
            "objective": "Verify the AgentMesh coordinated Compose path",
            "execution_mode": "COORDINATED",
            "max_concurrency": 2,
            "subtasks": [
                {"key": "left", "objective": "Produce left"},
                {"key": "right", "objective": "Produce right"},
                {
                    "key": "join",
                    "objective": "Join predecessor results",
                    "depends_on": ["left", "right"],
                },
            ],
        },
    )
    coordinated_task_id = str(coordinated["id"])
    request_json(
        f"/api/v1/tasks/{coordinated_task_id}/runs",
        method="POST",
        headers={
            "Idempotency-Key": f"compose-coordinated-{os.getenv('GITHUB_RUN_ID', 'local')}"
        },
    )
    coordinated_task = wait_for_task(coordinated_task_id)
    assert len(coordinated_task["subtasks"]) == 3
    assert all(subtask["status"] == "COMPLETED" for subtask in coordinated_task["subtasks"])
    assert [run["role"] for run in coordinated_task["runs"]].count("SUPERVISOR") == 1
    print(
        "Compose E2E passed for direct, reviewed, and coordinated Tasks: "
        f"{task_id}, {reviewed_task_id}, {coordinated_task_id}",
        flush=True,
    )


if __name__ == "__main__":
    main()
