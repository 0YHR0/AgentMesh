from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agentmesh.runtime_sdk import (
    DispatchReceipt,
    Envelope,
    LifecycleReceipt,
    ManagedAgentRuntime,
    RuntimeAssignment,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
    canonical_digest,
    canonical_json,
)
from agentmesh.runtime_sdk.models import (
    RuntimeContractError,
    UnknownMajorVersion,
    UnknownSecurityObligation,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_descriptor_fixture_round_trips_and_digest_is_stable() -> None:
    raw = load_fixture("descriptor.valid.json")
    descriptor = RuntimeDescriptor.from_dict(raw)

    assert descriptor.to_dict() == raw
    assert descriptor.digest() == "996f8dfa9d6873efaff3390533484efec181e06ad31861f4f856a1ccda061953"
    assert canonical_digest(raw) == canonical_digest(json.loads(canonical_json(raw)))


def test_assignment_fixture_round_trips_and_digest_is_stable() -> None:
    raw = load_fixture("assignment.valid.json")
    assignment = RuntimeAssignment.from_dict(raw)

    assert assignment.to_dict() == raw
    assert assignment.assignment_digest == assignment.digest()
    assert len(assignment.assignment_digest or "") == 64


def test_common_envelope_round_trip_and_secret_rejection() -> None:
    envelope = Envelope(
        schema_name="agentmesh.runtime.command",
        schema_version=1,
        message_id="00000000-0000-4000-8000-000000000010",
        tenant_id="tenant-default",
        occurred_at=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        producer="runtime-test",
        actor="principal-ref",
        correlation_id="00000000-0000-4000-8000-000000000011",
        idempotency_key="runtime-command:1",
        payload={"assignment_id": "00000000-0000-4000-8000-000000000001"},
    )
    assert Envelope.from_dict(envelope.to_dict()) == envelope
    with pytest.raises(RuntimeContractError):
        Envelope(
            schema_name="agentmesh.runtime.command",
            schema_version=1,
            message_id="00000000-0000-4000-8000-000000000010",
            tenant_id="tenant-default",
            occurred_at=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
            producer="runtime-test",
            actor="principal-ref",
            correlation_id="00000000-0000-4000-8000-000000000011",
            payload={"api_key": "must-not-cross"},
        )


@pytest.mark.parametrize(
    ("fixture", "error"),
    [
        ("assignment.unknown-major.json", UnknownMajorVersion),
        ("assignment.unknown-obligation.json", UnknownSecurityObligation),
        ("assignment.invalid-secret.json", RuntimeContractError),
    ],
)
def test_invalid_fixtures_fail_closed(fixture: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        RuntimeAssignment.from_dict(load_fixture(fixture))


def test_bounds_reject_deep_and_oversized_values() -> None:
    value: object = "leaf"
    for _ in range(33):
        value = {"nested": value}
    with pytest.raises(RuntimeContractError, match="nesting depth"):
        RuntimeDescriptor.from_dict(
            {
                **load_fixture("descriptor.valid.json"),
                "extensions": value,
            }
        )

    with pytest.raises(RuntimeContractError, match="size"):
        RuntimeDescriptor.from_dict(
            {
                **load_fixture("descriptor.valid.json"),
                "display_name": "x" * 262_145,
            }
        )


class FakeRuntime:
    """Minimal deterministic adapter used by the black-box contract skeleton."""

    def __init__(self) -> None:
        self._descriptor = RuntimeDescriptor(
            runtime_key="agentmesh.test.fake",
            display_name="Contract Fake",
            adapter_kind="test",
            capabilities=RuntimeCapabilities(execution_mode=("inline",), cancel="cooperative"),
        )
        self._dispatches: dict[str, DispatchReceipt] = {}
        self._canceled: dict[str, LifecycleReceipt] = {}

    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    def validate(self, assignment: RuntimeAssignment) -> ValidationReport:
        return ValidationReport(
            valid=assignment.execution_mode in self._descriptor.capabilities.execution_mode
        )

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt:
        if dispatch_key in self._dispatches:
            return self._dispatches[dispatch_key]
        execution_id = str(uuid4())
        handle = RuntimeExecutionHandle(
            runtime_execution_id=execution_id,
            runtime_version_id=assignment.runtime_version_id,
            provider_execution_ref=f"fake:{execution_id}",
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest or "",
            created_at=datetime.now(timezone.utc),
        )
        observation = RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=execution_id,
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest or "",
            provider_event_id="fake-accepted",
            phase=RuntimePhase.ACCEPTED,
            observed_at=datetime.now(timezone.utc),
        )
        receipt = DispatchReceipt(
            dispatch_key=dispatch_key,
            runtime_execution_id=execution_id,
            assignment_digest=assignment.assignment_digest or "",
            handle=handle,
            observation=observation,
        )
        self._dispatches[dispatch_key] = receipt
        return receipt

    def inspect(self, handle: RuntimeExecutionHandle) -> RuntimeObservation:
        return RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=handle.runtime_execution_id,
            assignment_id=handle.assignment_id,
            assignment_digest=handle.assignment_digest,
            provider_event_id="fake-succeeded",
            phase=RuntimePhase.SUCCEEDED,
            observed_at=datetime.now(timezone.utc),
            output={"ok": True},
        )

    def read_events(self, handle, *, cursor, limit):
        raise NotImplementedError

    def request_cancel(self, handle, *, cancellation_id, deadline):
        return self._canceled.setdefault(
            cancellation_id,
            LifecycleReceipt(
                operation_id=cancellation_id,
                runtime_execution_id=handle.runtime_execution_id,
                operation="cancel",
                accepted=True,
            ),
        )

    def request_pause(self, handle, *, operation_id):
        raise NotImplementedError

    def request_resume(self, handle, *, operation_id):
        raise NotImplementedError

    def close(self):
        return None


def test_fake_adapter_conformance_idempotency_and_late_terminal_observation() -> None:
    raw = load_fixture("assignment.valid.json")
    assignment = RuntimeAssignment.from_dict(raw)
    adapter: ManagedAgentRuntime = FakeRuntime()
    assert adapter.validate(assignment).valid
    first = adapter.dispatch(assignment, dispatch_key="runtime-dispatch:tenant-default:fixed")
    second = adapter.dispatch(assignment, dispatch_key="runtime-dispatch:tenant-default:fixed")
    assert first.to_dict() == second.to_dict()
    assert adapter.inspect(first.handle).phase is RuntimePhase.SUCCEEDED
