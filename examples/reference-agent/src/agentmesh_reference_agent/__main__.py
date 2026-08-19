"""Standalone JSONL reference Agent; only the public Runtime SDK is imported."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimeObservation,
    RuntimePhase,
    canonical_json,
    canonical_json_bytes,
)

MAX_LINE_BYTES = 262_144
MAX_FIXTURE_BYTES = 1_048_576


def _fixture(assignment: RuntimeAssignment) -> dict[str, Any]:
    candidate = (assignment.structured_input or {}).get("_reference_agent", {})
    if type(candidate) is not dict:
        raise ValueError("reference fixture must be an object")
    return candidate


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != {"schema", "operation", "assignment"}:
        raise ValueError("reference request contains unknown fields")
    if request["schema"] != "agentmesh.reference-agent.v1" or request["operation"] != "execute":
        raise ValueError("unsupported reference protocol operation")
    assignment = RuntimeAssignment.from_dict(request["assignment"])
    fixture = _fixture(assignment)
    stdout_bytes = fixture.get("stdout_bytes", 0)
    if (
        type(stdout_bytes) is not int
        or isinstance(stdout_bytes, bool)
        or not 0 <= stdout_bytes <= MAX_FIXTURE_BYTES
    ):
        raise ValueError("stdout_bytes is outside the controlled fixture range")
    stderr = fixture.get("stderr", "")
    if type(stderr) is not str or len(stderr.encode("utf-8")) > MAX_FIXTURE_BYTES:
        raise ValueError("stderr fixture is outside the controlled fixture range")
    if stderr:
        print(stderr, file=sys.stderr, end="", flush=True)
    if fixture.get("crash") is True:
        print("reference fixture crash: controlled", file=sys.stderr, flush=True)
        raise SystemExit(23)
    delay_ms = fixture.get("delay_ms", 0)
    if type(delay_ms) is not int or isinstance(delay_ms, bool) or not 0 <= delay_ms <= 60_000:
        raise ValueError("delay_ms is outside the controlled fixture range")
    child = None
    child_pid_file = fixture.get("child_pid_file")
    if fixture.get("spawn_tree") is True:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        if child_pid_file is not None:
            if type(child_pid_file) is not str or Path(child_pid_file).name != child_pid_file:
                raise ValueError("child_pid_file must be a safe basename")
            Path.cwd().joinpath(child_pid_file).write_text(str(child.pid), encoding="ascii")
    try:
        deadline = time.monotonic() + delay_ms / 1000
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                child.kill()
    report: dict[str, Any] = {
        "kind": "agentmesh.reference.report.v1",
        "assignment_digest": assignment.assignment_digest,
        "objective": assignment.objective,
        "input_digest": assignment.digest(),
    }
    env_keys = fixture.get("include_env", [])
    if env_keys:
        if (
            type(env_keys) is not list
            or any(type(key) is not str for key in env_keys)
            or len(env_keys) > 16
        ):
            raise ValueError("include_env fixture is invalid")
        report["environment"] = {key: os.environ.get(key) for key in env_keys}
    execution_id = assignment.correlation_ids.get("runtime_execution_id")
    if not isinstance(execution_id, str):
        raise ValueError("runtime execution identity is missing")
    observation = RuntimeObservation(
        observation_id=str(uuid5(NAMESPACE_URL, assignment.assignment_id + ":reference-result")),
        runtime_execution_id=execution_id,
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="reference-result",
        output=report,
    )
    response: dict[str, Any] = {
        "schema": "agentmesh.reference-agent.v1",
        "type": "result",
        "observation": observation.to_dict(),
        "artifact": {
            "name": "report.json",
            "media_type": "application/json",
            "content": canonical_json_bytes(report).decode("utf-8"),
        },
    }
    if fixture.get("malformed") is True:
        return {"_malformed": True}
    if stdout_bytes:
        response["_fixture_stdout_bytes"] = stdout_bytes
    return response


def main() -> int:
    line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
    if not line or len(line) > MAX_LINE_BYTES:
        return 2
    try:
        request = json.loads(line.decode("utf-8"))
        if type(request) is not dict:
            raise ValueError("request must be an object")
        response = _execute(request)
        padding = response.pop("_fixture_stdout_bytes", 0)
        if response.get("_malformed"):
            sys.stdout.write("not-json\n")
        else:
            sys.stdout.write(canonical_json(response))
            sys.stdout.write("x" * padding + "\n")
        sys.stdout.flush()
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"reference protocol error: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
