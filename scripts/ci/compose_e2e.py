from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("AGENTMESH_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


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
    print(f"Compose E2E passed for Task {task_id}", flush=True)


if __name__ == "__main__":
    main()
