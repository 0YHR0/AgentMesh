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
