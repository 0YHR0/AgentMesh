from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from redis import Redis
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.config import get_settings
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import InboxMessageRecord, OutboxEventRecord
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyInboxRepository
from agentmesh.maintenance.retention import (
    MessagingRetentionPolicy,
    MessagingRetentionService,
    RedisStreamRetentionStore,
    SqlAlchemyMessageRetentionStore,
    StreamRetentionPolicy,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_retention_cycle_lock_elects_one_database_session() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    store = SqlAlchemyMessageRetentionStore(session_factory)
    try:
        with store.try_cycle_lock() as first_acquired:
            assert first_acquired is True
            with store.try_cycle_lock() as second_acquired:
                assert second_acquired is False
    finally:
        engine.dispose()


def test_tenant_key_conflict_rejects_downgrade_without_schema_or_data_loss() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_ids = (f"migration-{uuid4().hex}", f"migration-{uuid4().hex}")
    consumer_name = f"migration-consumer-{uuid4().hex}"
    message_id = uuid4()
    alembic_config = Config("alembic.ini")
    try:
        with session_factory() as session, session.begin():
            for tenant_id in tenant_ids:
                session.add(
                    InboxMessageRecord(
                        tenant_id=tenant_id,
                        consumer_name=consumer_name,
                        message_id=message_id,
                        schema_name="agentmesh.test.migration",
                        schema_version=1,
                        processed_at=datetime.now(timezone.utc),
                    )
                )

        with pytest.raises(RuntimeError, match="multiple tenants share an Inbox"):
            command.downgrade(alembic_config, "20260717_0009")

        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20260720_0022"
            )
            assert inspect(connection).get_pk_constraint("inbox_messages")[
                "constrained_columns"
            ] == ["tenant_id", "consumer_name", "message_id"]
            assert (
                connection.scalar(
                    select(func.count(InboxMessageRecord.message_id)).where(
                        InboxMessageRecord.tenant_id.in_(tenant_ids)
                    )
                )
                == 2
            )

        with session_factory() as session, session.begin():
            session.execute(
                delete(InboxMessageRecord).where(InboxMessageRecord.tenant_id.in_(tenant_ids))
            )
        command.downgrade(alembic_config, "20260717_0009")
        with engine.connect() as connection:
            assert inspect(connection).get_pk_constraint("inbox_messages")[
                "constrained_columns"
            ] == ["consumer_name", "message_id"]
    finally:
        try:
            command.upgrade(alembic_config, "head")
            with session_factory() as session, session.begin():
                session.execute(
                    delete(InboxMessageRecord).where(InboxMessageRecord.tenant_id.in_(tenant_ids))
                )
        finally:
            engine.dispose()


def test_bounded_database_cleanup_and_pending_safe_stream_retention() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    tenant_id = f"retention-{uuid4().hex}"
    other_tenant_id = f"retention-{uuid4().hex}"
    consumer_name = f"retention-consumer-{uuid4().hex}"
    stream_name = f"agentmesh.test.retention.{uuid4().hex}"
    group_name = f"retention-group-{uuid4().hex}"
    old_published_ids = [uuid4(), uuid4()]
    pending_id = uuid4()
    quarantined_id = uuid4()
    recent_published_id = uuid4()
    old_inbox_id = uuid4()
    unrelated_old_inbox_id = uuid4()
    recent_inbox_id = uuid4()

    old_acked_stream_id = _stream_id(now - timedelta(days=40))
    recent_pending_stream_id = _stream_id(now - timedelta(days=1))
    old_acked_envelope = _message_envelope(tenant_id, uuid4(), now - timedelta(days=40))
    old_logical_pending_envelope = _message_envelope(
        tenant_id, old_inbox_id, now - timedelta(days=31)
    )

    try:
        with session_factory() as session, session.begin():
            session.add_all(
                [
                    _outbox_record(
                        event_id=old_published_ids[0],
                        tenant_id=tenant_id,
                        status="PUBLISHED",
                        created_at=now - timedelta(days=33),
                        published_at=now - timedelta(days=32),
                    ),
                    _outbox_record(
                        event_id=old_published_ids[1],
                        tenant_id=tenant_id,
                        status="PUBLISHED",
                        created_at=now - timedelta(days=32),
                        published_at=now - timedelta(days=31),
                    ),
                    _outbox_record(
                        event_id=pending_id,
                        tenant_id=tenant_id,
                        status="PENDING",
                        created_at=now - timedelta(days=20),
                    ),
                    _outbox_record(
                        event_id=quarantined_id,
                        tenant_id=tenant_id,
                        status="QUARANTINED",
                        created_at=now - timedelta(days=40),
                        quarantined_at=now - timedelta(days=39),
                    ),
                    _outbox_record(
                        event_id=recent_published_id,
                        tenant_id=tenant_id,
                        status="PUBLISHED",
                        created_at=now - timedelta(days=1),
                        published_at=now - timedelta(hours=23),
                    ),
                    InboxMessageRecord(
                        consumer_name=consumer_name,
                        message_id=old_inbox_id,
                        tenant_id=tenant_id,
                        schema_name="agentmesh.test.retention",
                        schema_version=1,
                        processed_at=now - timedelta(days=31),
                    ),
                    InboxMessageRecord(
                        consumer_name=consumer_name,
                        message_id=unrelated_old_inbox_id,
                        tenant_id=tenant_id,
                        schema_name="agentmesh.test.retention",
                        schema_version=1,
                        processed_at=now - timedelta(days=31),
                    ),
                    InboxMessageRecord(
                        consumer_name=consumer_name,
                        message_id=pending_id,
                        tenant_id=tenant_id,
                        schema_name="agentmesh.test.retention",
                        schema_version=1,
                        processed_at=now - timedelta(days=31),
                    ),
                    InboxMessageRecord(
                        consumer_name=consumer_name,
                        message_id=recent_inbox_id,
                        tenant_id=tenant_id,
                        schema_name="agentmesh.test.retention",
                        schema_version=1,
                        processed_at=now - timedelta(days=1),
                    ),
                    InboxMessageRecord(
                        consumer_name=consumer_name,
                        message_id=recent_inbox_id,
                        tenant_id=other_tenant_id,
                        schema_name="agentmesh.test.retention",
                        schema_version=1,
                        processed_at=now - timedelta(days=1),
                    ),
                ]
            )

        redis_client.xadd(
            stream_name,
            {"envelope": json.dumps(old_acked_envelope.to_dict())},
            id=old_acked_stream_id,
        )
        redis_client.xadd(
            stream_name,
            {"envelope": json.dumps(old_logical_pending_envelope.to_dict())},
            id=recent_pending_stream_id,
        )
        redis_client.xgroup_create(stream_name, group_name, id="0-0")
        delivered = redis_client.xreadgroup(
            group_name,
            "retention-consumer",
            {stream_name: ">"},
            count=2,
        )
        assert [message[0] for message in delivered[0][1]] == [
            old_acked_stream_id,
            recent_pending_stream_id,
        ]
        assert redis_client.xack(stream_name, group_name, old_acked_stream_id) == 1

        service = MessagingRetentionService(
            database=SqlAlchemyMessageRetentionStore(session_factory),
            streams=RedisStreamRetentionStore(redis_client),
            policy=MessagingRetentionPolicy(
                outbox_retention=timedelta(days=7),
                inbox_retention=timedelta(days=30),
                batch_size=1,
                streams=(
                    StreamRetentionPolicy(
                        stream_name=stream_name,
                        retention=timedelta(days=7),
                        max_entries=1,
                        required_group=group_name,
                        protects_inbox=True,
                    ),
                ),
            ),
        )

        first = service.run_once(now=now)

        assert first is not None
        assert first.database.outbox_deleted == 1
        assert first.database.inbox_deleted == 1
        assert first.database.inbox_cleanup_blocked is False
        assert first.database.inbox_guard_scan_complete is True
        assert first.database.inbox_guard_protected_messages == 1
        assert first.database.oldest_unsettled_delivery_age_seconds == pytest.approx(
            31 * 24 * 60 * 60
        )
        assert first.streams[0].deleted_entries == 1
        assert first.streams[0].pending_entries == 1
        assert first.streams[0].oldest_pending_age_seconds == pytest.approx(1 * 24 * 60 * 60)
        assert redis_client.xlen(stream_name) == 1
        assert redis_client.xrange(
            stream_name, min=recent_pending_stream_id, max=recent_pending_stream_id
        )[0][1] == {"envelope": json.dumps(old_logical_pending_envelope.to_dict())}
        assert (
            redis_client.xpending_range(
                stream_name,
                group_name,
                min="-",
                max="+",
                count=1,
            )[0]["message_id"]
            == recent_pending_stream_id
        )

        with session_factory() as session:
            remaining_old = session.scalar(
                select(func.count(OutboxEventRecord.id)).where(
                    OutboxEventRecord.id.in_(old_published_ids)
                )
            )
            assert remaining_old == 1
            assert session.get(OutboxEventRecord, pending_id) is not None
            assert session.get(OutboxEventRecord, quarantined_id) is not None
            assert session.get(OutboxEventRecord, recent_published_id) is not None
            inbox = SqlAlchemyInboxRepository(session)
            assert inbox.contains(tenant_id, consumer_name, old_inbox_id) is True
            assert inbox.contains(tenant_id, consumer_name, unrelated_old_inbox_id) is False
            assert inbox.contains(tenant_id, consumer_name, pending_id) is True
            assert inbox.contains(tenant_id, consumer_name, recent_inbox_id) is True
            assert inbox.contains(other_tenant_id, consumer_name, recent_inbox_id) is True

        assert redis_client.xack(stream_name, group_name, recent_pending_stream_id) == 1

        second = service.run_once(now=now)
        assert second is not None
        assert second.database.outbox_deleted == 1
        assert second.database.inbox_deleted == 1
        assert second.database.inbox_cleanup_blocked is False

        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(OutboxEventRecord.id)).where(
                        OutboxEventRecord.id.in_(old_published_ids)
                    )
                )
                == 0
            )
            assert SqlAlchemyInboxRepository(session).contains(
                tenant_id, consumer_name, recent_inbox_id
            )
            assert (
                SqlAlchemyInboxRepository(session).contains(tenant_id, consumer_name, old_inbox_id)
                is False
            )
            assert SqlAlchemyInboxRepository(session).contains(tenant_id, consumer_name, pending_id)
    finally:
        redis_client.delete(stream_name)
        redis_client.close()
        with session_factory() as session, session.begin():
            session.execute(
                delete(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id.in_([tenant_id, other_tenant_id])
                )
            )
            session.execute(
                delete(OutboxEventRecord).where(OutboxEventRecord.tenant_id == tenant_id)
            )
        engine.dispose()


def _outbox_record(
    *,
    event_id: UUID,
    tenant_id: str,
    status: str,
    created_at: datetime,
    published_at: datetime | None = None,
    quarantined_at: datetime | None = None,
) -> OutboxEventRecord:
    envelope = _message_envelope(tenant_id, event_id, created_at)
    return OutboxEventRecord(
        id=event_id,
        tenant_id=tenant_id,
        topic=envelope.schema_name,
        envelope=envelope.to_dict(),
        status=status,
        available_at=created_at,
        created_at=created_at,
        claimed_by=None,
        claimed_until=None,
        published_at=published_at,
        quarantined_at=quarantined_at,
        attempt_count=0,
        last_error=None,
    )


def _message_envelope(
    tenant_id: str,
    message_id: UUID,
    occurred_at: datetime,
) -> MessageEnvelope:
    return replace(
        MessageEnvelope.run_requested(
            tenant_id=tenant_id,
            task_id=uuid4(),
            run_id=uuid4(),
        ),
        message_id=message_id,
        occurred_at=occurred_at,
    )


def _stream_id(value: datetime) -> str:
    return f"{int(value.timestamp() * 1_000)}-0"
