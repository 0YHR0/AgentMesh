from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.config import get_settings
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import OutboxEventRecord
from agentmesh.messaging.outbox import OutboxRelay, SqlAlchemyOutboxStore

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


class _RecordingPublisher:
    def __init__(self) -> None:
        self.envelopes: list[MessageEnvelope] = []

    def publish(self, envelope: MessageEnvelope) -> str:
        self.envelopes.append(envelope)
        return f"recorded-{len(self.envelopes)}"


def test_relay_quarantines_malformed_row_and_publishes_valid_batch_peer() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    invalid_id = uuid4()
    valid = MessageEnvelope.run_requested(
        tenant_id=f"outbox-quarantine-{uuid4().hex}",
        task_id=uuid4(),
        run_id=uuid4(),
    )
    old_timestamp = datetime(2000, 1, 1, tzinfo=timezone.utc)

    try:
        with session_factory() as session, session.begin():
            session.add_all(
                [
                    _outbox_record(
                        event_id=invalid_id,
                        tenant_id=valid.tenant_id,
                        envelope={"schema_name": "not-an-envelope"},
                        created_at=old_timestamp,
                    ),
                    _outbox_record(
                        event_id=valid.message_id,
                        tenant_id=valid.tenant_id,
                        envelope=valid.to_dict(),
                        created_at=old_timestamp + timedelta(microseconds=1),
                    ),
                ]
            )

        publisher = _RecordingPublisher()
        relay = OutboxRelay(
            relay_id=f"quarantine-test-{uuid4().hex}",
            store=SqlAlchemyOutboxStore(session_factory),
            publisher=publisher,  # type: ignore[arg-type]
            batch_size=2,
            claim_duration=timedelta(seconds=30),
            retry_delay=timedelta(seconds=1),
        )

        assert relay.publish_once() == 1
        assert publisher.envelopes == [valid]

        with session_factory() as session:
            invalid = session.get(OutboxEventRecord, invalid_id)
            published = session.get(OutboxEventRecord, valid.message_id)

            assert invalid is not None
            assert invalid.status == "QUARANTINED"
            assert invalid.quarantined_at is not None
            assert invalid.claimed_by is None
            assert invalid.claimed_until is None
            assert invalid.attempt_count == 1
            assert invalid.last_error is not None
            assert invalid.last_error.startswith("EnvelopeDeserializationError: ")
            assert "not-an-envelope" not in invalid.last_error

            assert published is not None
            assert published.status == "PUBLISHED"
            assert published.published_at is not None
            assert published.quarantined_at is None
    finally:
        with session_factory() as session, session.begin():
            session.execute(
                delete(OutboxEventRecord).where(
                    OutboxEventRecord.id.in_([invalid_id, valid.message_id])
                )
            )
        engine.dispose()


def _outbox_record(
    *,
    event_id: UUID,
    tenant_id: str,
    envelope: dict[str, object],
    created_at: datetime,
) -> OutboxEventRecord:
    return OutboxEventRecord(
        id=event_id,
        tenant_id=tenant_id,
        topic="test.outbox.quarantine",
        envelope=envelope,
        status="PENDING",
        available_at=created_at,
        created_at=created_at,
        claimed_by=None,
        claimed_until=None,
        published_at=None,
        quarantined_at=None,
        attempt_count=0,
        last_error=None,
    )
