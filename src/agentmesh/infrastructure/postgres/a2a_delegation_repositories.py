from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus, RemoteTaskCorrelation
from agentmesh.infrastructure.postgres.models import RemoteTaskCorrelationRecord


class SqlAlchemyRemoteTaskCorrelationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, correlation: RemoteTaskCorrelation) -> None:
        self._session.add(_record(correlation))

    def get(
        self, correlation_id: UUID, *, for_update: bool = False
    ) -> RemoteTaskCorrelation | None:
        record = self._session.get(
            RemoteTaskCorrelationRecord, correlation_id, with_for_update=for_update
        )
        return _domain(record) if record is not None else None

    def get_for_task(self, task_id: UUID) -> RemoteTaskCorrelation | None:
        record = self._session.scalar(
            select(RemoteTaskCorrelationRecord).where(
                RemoteTaskCorrelationRecord.task_id == task_id
            )
        )
        return _domain(record) if record is not None else None

    def save(self, correlation: RemoteTaskCorrelation) -> None:
        record = self._session.get(RemoteTaskCorrelationRecord, correlation.id)
        if record is None:
            raise LookupError(correlation.id)
        record.status = correlation.status.value
        record.remote_task_id = correlation.remote_task_id
        record.remote_context_id = correlation.remote_context_id
        record.last_remote_state = correlation.last_remote_state
        record.last_response_digest = correlation.last_response_digest
        record.result = dict(correlation.result) if correlation.result is not None else None
        record.error = correlation.error
        record.poll_count = correlation.poll_count
        record.late_result = correlation.late_result
        record.updated_at = correlation.updated_at
        record.send_started_at = correlation.send_started_at
        record.terminal_at = correlation.terminal_at
        record.revision = correlation.revision

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[RemoteTaskCorrelation]:
        records = self._session.scalars(
            select(RemoteTaskCorrelationRecord)
            .where(RemoteTaskCorrelationRecord.tenant_id == tenant_id)
            .order_by(
                RemoteTaskCorrelationRecord.created_at.desc(),
                RemoteTaskCorrelationRecord.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return [_domain(record) for record in records]


def _record(value: RemoteTaskCorrelation) -> RemoteTaskCorrelationRecord:
    return RemoteTaskCorrelationRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        task_id=value.task_id,
        run_id=value.run_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        card_digest=value.card_digest,
        endpoint_url=value.endpoint_url,
        protocol_binding=value.protocol_binding,
        protocol_version=value.protocol_version,
        endpoint_tenant=value.endpoint_tenant,
        outbound_message_id=value.outbound_message_id,
        request_digest=value.request_digest,
        status=value.status.value,
        remote_task_id=value.remote_task_id,
        remote_context_id=value.remote_context_id,
        last_remote_state=value.last_remote_state,
        last_response_digest=value.last_response_digest,
        result=dict(value.result) if value.result is not None else None,
        error=value.error,
        poll_count=value.poll_count,
        late_result=value.late_result,
        created_at=value.created_at,
        updated_at=value.updated_at,
        send_started_at=value.send_started_at,
        terminal_at=value.terminal_at,
        revision=value.revision,
    )


def _domain(value: RemoteTaskCorrelationRecord) -> RemoteTaskCorrelation:
    return RemoteTaskCorrelation(
        id=value.id,
        tenant_id=value.tenant_id,
        task_id=value.task_id,
        run_id=value.run_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        card_digest=value.card_digest,
        endpoint_url=value.endpoint_url,
        protocol_binding=value.protocol_binding,
        protocol_version=value.protocol_version,
        endpoint_tenant=value.endpoint_tenant,
        outbound_message_id=value.outbound_message_id,
        request_digest=value.request_digest,
        status=RemoteCorrelationStatus(value.status),
        remote_task_id=value.remote_task_id,
        remote_context_id=value.remote_context_id,
        last_remote_state=value.last_remote_state,
        last_response_digest=value.last_response_digest,
        result=dict(value.result) if value.result is not None else None,
        error=value.error,
        poll_count=value.poll_count,
        late_result=value.late_result,
        created_at=value.created_at,
        updated_at=value.updated_at,
        send_started_at=value.send_started_at,
        terminal_at=value.terminal_at,
        revision=value.revision,
    )
