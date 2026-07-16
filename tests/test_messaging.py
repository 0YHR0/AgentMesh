from datetime import timezone
from uuid import uuid4

from agentmesh.domain.messaging import MessageEnvelope


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
