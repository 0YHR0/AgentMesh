from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_lifecycle_services import (
    RuntimeLifecycleService,
    _valid_receipt,
)
from agentmesh.application.runtime_snapshots import handle_from_snapshot, handle_snapshot_for
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import LifecycleReceipt, RuntimeExecutionHandle, RuntimePhase


class _Repo:
    def __init__(self, lifecycle, execution, handle_snapshot=None):
        self.lifecycle = lifecycle
        self.execution = execution
        self.handle_snapshot = handle_snapshot
        self.inbox = _Inbox()

    def get_handle_snapshot(self, execution_id, *, tenant_id):
        return self.handle_snapshot

    def get_execution(self, execution_id, *, tenant_id, for_update=False):
        return self.execution if execution_id == self.execution.id else None

    def claim_due_lifecycle(
        self,
        *,
        tenant_id,
        now,
        lease,
        execution_id=None,
        operation_id=None,
        has_handle,
    ):
        value = self.lifecycle
        if value is None or value.status is not RuntimeLifecycleStatus.REQUESTED:
            return None
        if execution_id is not None and value.runtime_execution_id != execution_id:
            return None
        if operation_id is not None and value.operation_id != operation_id:
            return None
        if value.next_attempt_at is not None and value.next_attempt_at > now:
            return None
        self.lifecycle = (
            value.claim_for_provider(now=now, lease=lease)
            if has_handle
            else value.schedule_retry(now=now, error_code="runtime.handle_unavailable")
        )
        return self.lifecycle

    def list_due_lifecycle_refs(self, *, tenant_id, now, limit=32):
        if self.lifecycle is not None and self.lifecycle.next_attempt_at <= now:
            return [(self.lifecycle.runtime_execution_id, self.lifecycle.operation_id)]
        return []

    def find_lifecycle_operation(self, execution_id, *, tenant_id, operation_id, for_update=False):
        if self.lifecycle is not None and self.lifecycle.operation_id == operation_id:
            return self.lifecycle
        return None

    def save_lifecycle_operation(self, value):
        self.lifecycle = value


class _Uow:
    def __init__(self, repo):
        self.runtimes = repo
        self.inbox = repo.inbox

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def commit(self):
        return None


class _Inbox:
    def __init__(self):
        self.message_ids = set()

    def contains(self, tenant_id, consumer_name, message_id):
        return (tenant_id, consumer_name, message_id) in self.message_ids

    def add(self, message):
        self.message_ids.add((message.tenant_id, message.consumer_name, message.message_id))


class _Adapter:
    def __init__(self, receipt):
        self.receipt = receipt
        self.calls = 0
        self.deadlines = []
        self.timeouts = []
        self.raise_timeout = False
        self.lose_response_once = False
        self.effect_applied = False
        self.effect_calls = 0

    def request_cancel(self, handle, *, cancellation_id, deadline, timeout=None):
        self.calls += 1
        self.deadlines.append(deadline)
        self.timeouts.append(timeout)
        if not self.effect_applied:
            self.effect_applied = True
            self.effect_calls += 1
        if self.lose_response_once:
            self.lose_response_once = False
            raise TimeoutError("response lost after cancellation effect")
        if self.raise_timeout:
            raise TimeoutError("transport timed out")
        return self.receipt


def _fixture():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    execution = RuntimeExecution.prepare(
        tenant_id="tenant-a",
        run_id=uuid4(),
        runtime_version_id=uuid4(),
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key="dispatch",
        dispatch_digest="b" * 64,
        now=now,
    )
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(execution.id),
        runtime_version_id=str(execution.runtime_version_id),
        provider_execution_ref="provider-ref",
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        created_at=now,
    )
    lifecycle = RuntimeLifecycleIntent(
        id=uuid4(),
        tenant_id="tenant-a",
        runtime_execution_id=execution.id,
        operation_id=f"runtime-cancel:{execution.id}:v1",
        operation=RuntimeLifecycleOperation.CANCEL,
        intent_digest="c" * 64,
        status=RuntimeLifecycleStatus.REQUESTED,
        deadline=now + timedelta(minutes=5),
        receipt_summary=None,
        version=1,
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
    )
    repo = _Repo(lifecycle, execution, handle_snapshot_for(handle, tenant_id="tenant-a"))
    return now, repo


def test_lifecycle_receipt_is_idempotent_and_replay_does_not_call_provider_again():
    now, repo = _fixture()
    receipt = LifecycleReceipt(
        operation_id=repo.lifecycle.operation_id,
        runtime_execution_id=str(repo.execution.id),
        operation="cancel",
        accepted=True,
        observed_phase=RuntimePhase.CANCEL_REQUESTED,
    )
    adapter = _Adapter(receipt)
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=10),
    )

    first = service.process_due(repo.execution.id, now=now)
    second = service.process_due(repo.execution.id, now=now + timedelta(seconds=1))

    assert first.provider_called is True
    assert second.provider_called is False
    assert adapter.calls == 1
    assert adapter.deadlines == [now + timedelta(minutes=5)]
    assert adapter.timeouts[0] < timedelta(seconds=30)
    assert adapter.timeouts[0] <= timedelta(seconds=10)
    assert repo.lifecycle.status is RuntimeLifecycleStatus.ACCEPTED
    assert repo.lifecycle.receipt_summary["accepted"] is True


def test_invalid_receipt_releases_claim_with_one_second_backoff():
    now, repo = _fixture()
    adapter = _Adapter(
        LifecycleReceipt(
            operation_id="wrong-operation",
            runtime_execution_id=str(repo.execution.id),
            operation="cancel",
            accepted=True,
            observed_phase=RuntimePhase.CANCEL_REQUESTED,
        )
    )
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
    )

    service.process_due(repo.execution.id, now=now)

    assert repo.lifecycle.status is RuntimeLifecycleStatus.REQUESTED
    assert repo.lifecycle.attempt_count == 1
    assert repo.lifecycle.next_attempt_at == now + timedelta(seconds=1)
    assert repo.lifecycle.last_error_code == "runtime.lifecycle_receipt_invalid"


@pytest.mark.parametrize("phase", list(RuntimePhase))
@pytest.mark.parametrize("accepted", [True, False])
def test_cancel_receipt_matrix_is_closed(phase, accepted):
    _, repo = _fixture()
    receipt = LifecycleReceipt(
        operation_id=repo.lifecycle.operation_id,
        runtime_execution_id=str(repo.execution.id),
        operation="cancel",
        accepted=accepted,
        observed_phase=phase,
    )
    crossed = {
        RuntimePhase.DISPATCHING,
        RuntimePhase.ACCEPTED,
        RuntimePhase.RUNNING,
        RuntimePhase.WAITING_INPUT,
        RuntimePhase.WAITING_APPROVAL,
        RuntimePhase.PAUSE_REQUESTED,
        RuntimePhase.PAUSED,
    }
    expected = phase.terminal or (
        not accepted and phase in crossed
    ) or (accepted and phase is RuntimePhase.CANCEL_REQUESTED)
    assert _valid_receipt(receipt, repo.lifecycle) is expected


def test_cancel_receipt_identity_and_missing_phase_fail_closed():
    _, repo = _fixture()
    base = {
        "operation_id": repo.lifecycle.operation_id,
        "runtime_execution_id": str(repo.execution.id),
        "operation": "cancel",
        "accepted": True,
        "observed_phase": RuntimePhase.CANCEL_REQUESTED,
    }
    for field, value in (
        ("operation_id", "wrong-operation"),
        ("runtime_execution_id", str(uuid4())),
        ("operation", "pause"),
    ):
        assert not _valid_receipt(LifecycleReceipt(**{**base, field: value}), repo.lifecycle)
    assert not _valid_receipt(
        LifecycleReceipt(**{**base, "observed_phase": None}), repo.lifecycle
    )


def test_response_loss_replays_same_cancel_operation_without_duplicate_effect():
    now, repo = _fixture()
    receipt = LifecycleReceipt(
        operation_id=repo.lifecycle.operation_id,
        runtime_execution_id=str(repo.execution.id),
        operation="cancel",
        accepted=True,
        observed_phase=RuntimePhase.CANCEL_REQUESTED,
    )
    adapter = _Adapter(receipt)
    adapter.lose_response_once = True
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
    )

    first = service.process_due(repo.execution.id, now=now)
    second = service.process_due(repo.execution.id, now=now + timedelta(seconds=1))

    assert first.provider_called is True
    assert second.provider_called is True
    assert adapter.calls == 2
    assert adapter.effect_calls == 1
    assert repo.lifecycle.status is RuntimeLifecycleStatus.ACCEPTED
    assert repo.lifecycle.receipt_summary["operation_id"] == repo.lifecycle.operation_id
    assert adapter.deadlines == [now + timedelta(minutes=5)] * 2


def test_lifecycle_envelope_replay_uses_inbox_dedup_and_invalid_envelopes_fail_closed():
    now, repo = _fixture()
    receipt = LifecycleReceipt(
        operation_id=repo.lifecycle.operation_id,
        runtime_execution_id=str(repo.execution.id),
        operation="cancel",
        accepted=True,
        observed_phase=RuntimePhase.CANCEL_REQUESTED,
    )
    adapter = _Adapter(receipt)
    clock_values = iter((now, now, now))
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        clock=lambda: next(clock_values),
    )
    envelope = MessageEnvelope.domain_event(
        schema_name="agentmesh.runtime.lifecycle.requested",
        tenant_id="tenant-a",
        aggregate_id=repo.execution.id,
        payload={
            "runtime_execution_id": str(repo.execution.id),
            "operation_id": repo.lifecycle.operation_id,
        },
    )

    first = service.process_envelope(envelope)
    replay = service.process_envelope(envelope)

    assert first.provider_called is True
    assert replay.provider_called is False
    assert adapter.calls == 1
    assert len(repo.inbox.message_ids) == 1

    with pytest.raises(InvalidTaskInput):
        service.process_envelope(replace(envelope, schema_name="invalid.schema"))
    with pytest.raises(InvalidTaskInput):
        service.process_envelope(replace(envelope, tenant_id="tenant-b"))
    with pytest.raises(InvalidTaskInput):
        service.process_envelope(
            replace(envelope, payload={"runtime_execution_id": str(repo.execution.id)})
        )


def test_missing_handle_retries_without_provider_call_and_preserves_attempt_count():
    now, repo = _fixture()
    repo.handle_snapshot = None
    adapter = _Adapter(None)
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
    )

    result = service.process_due(repo.execution.id, now=now)

    assert result.provider_called is False
    assert adapter.calls == 0
    assert repo.lifecycle.attempt_count == 0
    assert repo.lifecycle.next_attempt_at == now + timedelta(seconds=1)


def test_mismatched_handle_retries_without_provider_call_and_preserves_attempt_count():
    now, repo = _fixture()
    handle = handle_from_snapshot(repo.handle_snapshot)
    repo.handle_snapshot = handle_snapshot_for(
        replace(handle, assignment_id=str(uuid4())), tenant_id="tenant-a"
    )
    adapter = _Adapter(None)
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
    )

    result = service.process_due(repo.execution.id, now=now)

    assert result.provider_called is False
    assert adapter.calls == 0
    assert repo.lifecycle.attempt_count == 0
    assert repo.lifecycle.next_attempt_at == now + timedelta(seconds=1)


def test_transport_timeout_is_separate_from_business_deadline_and_retries_same_operation():
    now, repo = _fixture()
    adapter = _Adapter(
        LifecycleReceipt(
            operation_id=repo.lifecycle.operation_id,
            runtime_execution_id=str(repo.execution.id),
            operation="cancel",
            accepted=True,
            observed_phase=RuntimePhase.CANCEL_REQUESTED,
        )
    )
    adapter.raise_timeout = True
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=10),
    )

    result = service.process_due(repo.execution.id, now=now)

    assert result.provider_called is True
    assert adapter.calls == 1
    assert adapter.deadlines == [now + timedelta(minutes=5)]
    assert adapter.timeouts[0] < timedelta(seconds=30)
    assert repo.lifecycle.status is RuntimeLifecycleStatus.REQUESTED
    assert repo.lifecycle.attempt_count == 1
    assert repo.lifecycle.receipt_summary is None
    assert repo.lifecycle.operation_id == f"runtime-cancel:{repo.execution.id}:v1"
    assert repo.lifecycle.last_error_code == "runtime.lifecycle_timeout"


def test_transport_timeout_uses_actual_remaining_claim_after_pre_call_delay():
    now, repo = _fixture()
    adapter = _Adapter(
        LifecycleReceipt(
            operation_id=repo.lifecycle.operation_id,
            runtime_execution_id=str(repo.execution.id),
            operation="cancel",
            accepted=True,
            observed_phase=RuntimePhase.CANCEL_REQUESTED,
        )
    )
    clock_values = iter((now, now + timedelta(seconds=5), now + timedelta(seconds=5)))
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=29, microseconds=999999),
        clock=lambda: next(clock_values),
    )

    service.process_due(repo.execution.id)

    assert adapter.timeouts[0] < timedelta(seconds=25)
    assert adapter.deadlines == [now + timedelta(minutes=5)]


def test_late_provider_return_after_claim_expiry_does_not_write_receipt():
    now, repo = _fixture()
    adapter = _Adapter(
        LifecycleReceipt(
            operation_id=repo.lifecycle.operation_id,
            runtime_execution_id=str(repo.execution.id),
            operation="cancel",
            accepted=True,
            observed_phase=RuntimePhase.CANCEL_REQUESTED,
        )
    )
    clock_values = iter((now, now, now + timedelta(seconds=31)))
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=10),
        clock=lambda: next(clock_values),
    )

    result = service.process_due(repo.execution.id)

    assert result.provider_called is True
    assert adapter.calls == 1
    assert repo.lifecycle.status is RuntimeLifecycleStatus.REQUESTED
    assert repo.lifecycle.receipt_summary is None
    assert repo.lifecycle.claim_token is not None


def test_transport_failure_backoff_uses_completion_timestamp():
    now, repo = _fixture()
    adapter = _Adapter(None)
    adapter.raise_timeout = True
    clock_values = iter((now, now, now + timedelta(seconds=2)))
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=10),
        clock=lambda: next(clock_values),
    )

    service.process_due(repo.execution.id)

    assert repo.lifecycle.next_attempt_at == now + timedelta(seconds=3)


def test_expired_transport_budget_releases_claim_without_provider_call():
    now, repo = _fixture()
    repo.lifecycle = replace(repo.lifecycle, deadline=now + timedelta(microseconds=500))
    adapter = _Adapter(None)
    service = RuntimeLifecycleService(
        uow_factory=lambda: _Uow(repo),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
        adapter=adapter,
        claim_lease=timedelta(seconds=30),
        adapter_timeout=timedelta(seconds=10),
    )

    result = service.process_due(repo.execution.id, now=now)

    assert result.provider_called is False
    assert adapter.calls == 0
    assert repo.lifecycle.attempt_count == 0
    assert repo.lifecycle.status is RuntimeLifecycleStatus.REQUESTED
    assert repo.lifecycle.last_error_code == "runtime.lifecycle_timeout"
