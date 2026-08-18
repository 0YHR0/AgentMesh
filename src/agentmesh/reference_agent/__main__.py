"""Run the reference Agent's bounded JSON-lines protocol."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
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


def _fixture(assignment: RuntimeAssignment) -> dict[str, Any]:
    value = assignment.structured_input or {}
    candidate = value.get("_reference_agent")
    if candidate is None:
        return {}
    if type(candidate) is not dict:
        raise ValueError("reference fixture must be an object")
    return candidate


def _report(assignment: RuntimeAssignment) -> dict[str, Any]:
    """Return a stable report payload for parity and replay tests."""

    return {
        "kind": "agentmesh.reference.report.v1",
        "assignment_digest": assignment.assignment_digest,
        "objective": assignment.objective,
        "input_digest": assignment.digest(),
    }


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("schema") != "agentmesh.reference-agent.v1":
        raise ValueError("unsupported reference protocol schema")
    if request.get("operation") != "execute":
        raise ValueError("unsupported reference operation")
    assignment = RuntimeAssignment.from_dict(request.get("assignment"))
    fixture = _fixture(assignment)
    if fixture.get("crash") is True:
        print("reference fixture crash: controlled", file=sys.stderr, flush=True)
        raise SystemExit(23)
    delay_ms = fixture.get("delay_ms", 0)
    if type(delay_ms) is not int or isinstance(delay_ms, bool) or not 0 <= delay_ms <= 60_000:
        raise ValueError("delay_ms is outside the controlled fixture range")
    deadline = time.monotonic() + delay_ms / 1000
    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    report = _report(assignment)
    artifact_content = canonical_json_bytes(report)
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
            "content": artifact_content.decode("utf-8"),
        },
    }
    if fixture.get("malformed") is True:
        return {"_malformed": True}
    if type(fixture.get("stdout_bytes")) is int:
        response["padding"] = "x" * fixture["stdout_bytes"]
    return response


def main() -> int:
    line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
    if not line:
        return 2
    if len(line) > MAX_LINE_BYTES:
        print("reference protocol assignment exceeds limit", file=sys.stderr)
        return 2
    try:
        request = json.loads(line.decode("utf-8"))
        if type(request) is not dict:
            raise ValueError("request must be an object")
        response = _execute(request)
        if response.get("_malformed"):
            sys.stdout.write("not-json\n")
        else:
            sys.stdout.write(canonical_json(response) + "\n")
        sys.stdout.flush()
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # bounded, non-sensitive protocol error
        print(f"reference protocol error: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
