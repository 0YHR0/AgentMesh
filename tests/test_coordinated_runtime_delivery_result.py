from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedRuntimeDeliveryResult,
    CoordinatedRuntimeDeliveryResultKind,
    DeliveryInProgress,
)
from agentmesh.domain.errors import InvalidTaskInput


def _ids():
    return "tenant-a", uuid4(), uuid4(), uuid4(), uuid4()


@pytest.mark.parametrize(
    ("kind", "attempt", "execution", "reason"),
    [
        (CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE, None, None, None),
        (CoordinatedRuntimeDeliveryResultKind.REPLAY, None, None, None),
        (CoordinatedRuntimeDeliveryResultKind.BLOCKED_BY_DRAIN, None, None, "coordination.drain"),
    ],
)
def test_routing_results_are_identity_only(kind, attempt, execution, reason):
    tenant, task, run, _, _ = _ids()
    result = CoordinatedRuntimeDeliveryResult(
        kind=kind,
        tenant_id=tenant,
        task_id=task,
        run_id=run,
        attempt_id=attempt,
        execution_id=execution,
        reason=reason,
    )
    assert CoordinatedRuntimeDeliveryResult.from_dict(result.to_dict()) == result


def test_processed_allows_no_execution_for_pre_dispatch_failure():
    tenant, task, run, attempt, _ = _ids()
    result = CoordinatedRuntimeDeliveryResult.processed(
        tenant_id=tenant, task_id=task, run_id=run, attempt_id=attempt
    )
    assert result.execution_id is None
    assert CoordinatedRuntimeDeliveryResult.from_dict(result.to_dict()) == result


def test_unknown_requires_ownership_and_safe_reason():
    tenant, task, run, attempt, execution = _ids()
    result = CoordinatedRuntimeDeliveryResult.parked_unknown(
        tenant_id=tenant,
        task_id=task,
        run_id=run,
        attempt_id=attempt,
        execution_id=execution,
        reason="runtime.outcome_unknown",
    )
    assert result.runtime_execution_id == execution
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult(
            kind=CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN,
            tenant_id=tenant,
            task_id=task,
            run_id=run,
            attempt_id=attempt,
            execution_id=execution,
        )


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        (CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE, {"attempt_id": "attempt"}),
        (CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE, {"execution_id": "execution"}),
        (CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE, {"reason": "routing.reason"}),
        (CoordinatedRuntimeDeliveryResultKind.REPLAY, {"attempt_id": "attempt"}),
        (CoordinatedRuntimeDeliveryResultKind.REPLAY, {"execution_id": "execution"}),
        (CoordinatedRuntimeDeliveryResultKind.REPLAY, {"reason": "replay.reason"}),
        (CoordinatedRuntimeDeliveryResultKind.BLOCKED_BY_DRAIN, {"attempt_id": "attempt"}),
        (CoordinatedRuntimeDeliveryResultKind.BLOCKED_BY_DRAIN, {"execution_id": "execution"}),
        (CoordinatedRuntimeDeliveryResultKind.PROCESSED, {}),
        (CoordinatedRuntimeDeliveryResultKind.PROCESSED, {"reason": "runtime.failure"}),
        (CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN, {"reason": "runtime.unknown"}),
        (CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN, {"attempt_id": None}),
        (CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN, {"execution_id": None}),
        (CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN, {"reason": None}),
    ],
)
def test_result_kind_rejects_invalid_ownership_shape(kind, fields):
    tenant, task, run, attempt, execution = _ids()
    values = {
        "kind": kind,
        "tenant_id": tenant,
        "task_id": task,
        "run_id": run,
        "attempt_id": attempt if kind is CoordinatedRuntimeDeliveryResultKind.PROCESSED else None,
        "execution_id": execution
        if kind is CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN
        else None,
        "reason": "runtime.unknown"
        if kind is CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN
        else None,
    }
    if kind is CoordinatedRuntimeDeliveryResultKind.PROCESSED and not fields:
        values["attempt_id"] = None
    values.update(
        {
            key: (
                None
                if value is None
                else {
                    "attempt_id": attempt,
                    "execution_id": execution,
                    "reason": value,
                }.get(key, value)
            )
            for key, value in fields.items()
        }
    )
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult(**values)


@pytest.mark.parametrize(
    "missing",
    ["schema_name", "schema_version", "kind", "tenant_id", "task_id", "run_id"],
)
def test_result_deserialization_requires_closed_required_fields(missing):
    tenant, task, run, attempt, execution = _ids()
    payload = CoordinatedRuntimeDeliveryResult.parked_unknown(
        tenant_id=tenant,
        task_id=task,
        run_id=run,
        attempt_id=attempt,
        execution_id=execution,
        reason="runtime.outcome_unknown",
    ).to_dict()
    del payload[missing]
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult.from_dict(payload)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.update({"kind": "UNKNOWN"}),
        lambda payload: payload.update({"task_id": str(uuid4()).upper()}),
    ],
)
def test_result_deserialization_rejects_unknown_kind_and_noncanonical_uuid(mutator):
    tenant, task, run, attempt, execution = _ids()
    payload = CoordinatedRuntimeDeliveryResult.parked_unknown(
        tenant_id=tenant,
        task_id=task,
        run_id=run,
        attempt_id=attempt,
        execution_id=execution,
        reason="runtime.outcome_unknown",
    ).to_dict()
    mutator(payload)
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult.from_dict(payload)


def test_result_deserialization_rejects_non_mapping():
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult.from_dict([])


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": True},
        {"task_id": str(uuid4()).upper()},
        {"reason": "provider_secret"},
        {"reason": "runtime/unknown"},
    ],
)
def test_result_deserialization_rejects_mutated_or_unsafe_fields(payload):
    tenant, task, run, attempt, execution = _ids()
    result = CoordinatedRuntimeDeliveryResult.parked_unknown(
        tenant_id=tenant,
        task_id=task,
        run_id=run,
        attempt_id=attempt,
        execution_id=execution,
        reason="runtime.outcome_unknown",
    )
    mutated = result.to_dict()
    mutated.update(payload)
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryResult.from_dict(mutated)


def test_delivery_in_progress_is_retryable_and_pairs_identities():
    tenant, task, run, _, _ = _ids()
    del tenant
    retry = DeliveryInProgress(task, run)
    assert retry.task_id == task
    assert retry.run_id == run
    assert "provider" not in str(retry).casefold()
    with pytest.raises(InvalidTaskInput):
        DeliveryInProgress(task_id=task)
