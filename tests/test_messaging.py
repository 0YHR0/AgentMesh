from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import OutboxEventRecord
from agentmesh.messaging.outbox import SqlAlchemyOutboxStore


def test_message_envelope_round_trip() -> None:
    envelope = MessageEnvelope.run_requested(
        tenant_id="test-tenant",
        task_id=uuid4(),
        run_id=uuid4(),
    )

    restored = MessageEnvelope.from_dict(envelope.to_dict())

    assert restored == envelope


def test_message_envelope_assumes_utc_for_legacy_naive_timestamp() -> None:
    envelope = MessageEnvelope.run_requested(
        tenant_id="test-tenant",
        task_id=uuid4(),
        run_id=uuid4(),
    )
    serialized = envelope.to_dict()
    serialized["occurred_at"] = "2026-07-16T12:00:00"

    restored = MessageEnvelope.from_dict(serialized)

    assert restored.occurred_at.tzinfo is timezone.utc


def test_claim_record_quarantines_malformed_envelope_without_payload_error() -> None:
    claimed_at = datetime.now(timezone.utc)
    record = _outbox_record({"schema_name": "sensitive-invalid-payload"})

    claimed = SqlAlchemyOutboxStore._claim_record(
        record,
        relay_id="relay-test",
        claimed_at=claimed_at,
        claim_duration=timedelta(seconds=30),
    )

    assert claimed is None
    assert record.status == "QUARANTINED"
    assert record.quarantined_at == claimed_at
    assert record.claimed_by is None
    assert record.claimed_until is None
    assert record.attempt_count == 1
    assert record.last_error is not None
    assert record.last_error.startswith("EnvelopeDeserializationError: ")
    assert "sensitive-invalid-payload" not in record.last_error


def test_claim_record_returns_valid_envelope_with_active_claim() -> None:
    claimed_at = datetime.now(timezone.utc)
    envelope = MessageEnvelope.run_requested(
        tenant_id="test-tenant",
        task_id=uuid4(),
        run_id=uuid4(),
    )
    record = _outbox_record(envelope.to_dict(), event_id=envelope.message_id)

    claimed = SqlAlchemyOutboxStore._claim_record(
        record,
        relay_id="relay-test",
        claimed_at=claimed_at,
        claim_duration=timedelta(seconds=30),
    )

    assert claimed is not None
    assert claimed.id == envelope.message_id
    assert claimed.envelope == envelope
    assert record.status == "PENDING"
    assert record.claimed_by == "relay-test"
    assert record.claimed_until == claimed_at + timedelta(seconds=30)
    assert record.quarantined_at is None
    assert record.attempt_count == 1


def _outbox_record(
    envelope: dict[str, object],
    *,
    event_id: UUID | None = None,
) -> OutboxEventRecord:
    now = datetime.now(timezone.utc)
    return OutboxEventRecord(
        id=event_id or uuid4(),
        tenant_id="test-tenant",
        topic="test.topic",
        envelope=envelope,
        status="PENDING",
        available_at=now,
        created_at=now,
        claimed_by=None,
        claimed_until=None,
        published_at=None,
        quarantined_at=None,
        attempt_count=0,
        last_error=None,
    )
