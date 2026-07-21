from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.tools import (
    ToolAuthorizationStatus,
    ToolExecutionAuthorization,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSideEffect,
)
from agentmesh.infrastructure.postgres.models import (
    ToolExecutionAuthorizationRecord,
    ToolInvocationRecord,
)


class SqlAlchemyToolInvocationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, invocation: ToolInvocation) -> None:
        self._session.add(self._to_record(invocation))

    def get(
        self,
        invocation_id: UUID,
        *,
        for_update: bool = False,
    ) -> ToolInvocation | None:
        statement = select(ToolInvocationRecord).where(ToolInvocationRecord.id == invocation_id)
        if for_update:
            statement = statement.with_for_update()
        record = self._session.execute(statement).scalar_one_or_none()
        return self._to_domain(record) if record is not None else None

    def save(self, invocation: ToolInvocation) -> None:
        record = self._session.get(ToolInvocationRecord, invocation.id)
        if record is None:
            raise LookupError(invocation.id)
        record.protocol_version = invocation.protocol_version
        record.schema_digest = invocation.schema_digest
        record.status = invocation.status.value
        record.result_digest = invocation.result_digest
        record.result_bytes = invocation.result_bytes
        record.error = invocation.error
        record.completed_at = invocation.completed_at

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]:
        statement = (
            select(ToolInvocationRecord)
            .where(ToolInvocationRecord.task_id == task_id)
            .order_by(ToolInvocationRecord.started_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(value: ToolInvocation) -> ToolInvocationRecord:
        return ToolInvocationRecord(
            id=value.id,
            tenant_id=value.tenant_id,
            task_id=value.task_id,
            run_id=value.run_id,
            server_name=value.server_name,
            tool_key=value.tool_key,
            tool_name=value.tool_name,
            side_effect=value.side_effect.value,
            protocol_version=value.protocol_version,
            schema_digest=value.schema_digest,
            arguments_digest=value.arguments_digest,
            status=value.status.value,
            result_digest=value.result_digest,
            result_bytes=value.result_bytes,
            error=value.error,
            started_at=value.started_at,
            completed_at=value.completed_at,
        )

    @staticmethod
    def _to_domain(value: ToolInvocationRecord) -> ToolInvocation:
        return ToolInvocation(
            id=value.id,
            tenant_id=value.tenant_id,
            task_id=value.task_id,
            run_id=value.run_id,
            server_name=value.server_name,
            tool_key=value.tool_key,
            tool_name=value.tool_name,
            side_effect=ToolSideEffect(value.side_effect),
            protocol_version=value.protocol_version,
            schema_digest=value.schema_digest,
            arguments_digest=value.arguments_digest,
            status=ToolInvocationStatus(value.status),
            result_digest=value.result_digest,
            result_bytes=value.result_bytes,
            error=value.error,
            started_at=value.started_at,
            completed_at=value.completed_at,
        )


class SqlAlchemyToolExecutionAuthorizationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, value: ToolExecutionAuthorization) -> None:
        self._session.add(ToolExecutionAuthorizationRecord(**self._values(value)))

    def get_for_task(
        self, task_id: UUID, *, for_update: bool = False
    ) -> ToolExecutionAuthorization | None:
        statement = select(ToolExecutionAuthorizationRecord).where(
            ToolExecutionAuthorizationRecord.task_id == task_id
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.execute(statement).scalar_one_or_none()
        return self._to_domain(record) if record is not None else None

    def save(self, value: ToolExecutionAuthorization) -> None:
        record = self._session.get(ToolExecutionAuthorizationRecord, value.id)
        if record is None:
            raise LookupError(value.id)
        record.status = value.status.value
        record.invocation_id = value.invocation_id
        record.completed_at = value.completed_at

    @staticmethod
    def _values(value: ToolExecutionAuthorization) -> dict:
        return {
            "id": value.id, "tenant_id": value.tenant_id, "task_id": value.task_id,
            "governed_action_id": value.governed_action_id,
            "principal_id": value.principal_id, "server_id": value.server_id,
            "server_version_id": value.server_version_id,
            "configuration_digest": value.configuration_digest,
            "tool_key": value.tool_key, "tool_name": value.tool_name,
            "side_effect": value.side_effect.value, "schema_digest": value.schema_digest,
            "arguments_digest": value.arguments_digest,
            "idempotency_key_digest": value.idempotency_key_digest,
            "status": value.status.value, "invocation_id": value.invocation_id,
            "created_at": value.created_at, "completed_at": value.completed_at,
        }

    @staticmethod
    def _to_domain(value: ToolExecutionAuthorizationRecord) -> ToolExecutionAuthorization:
        return ToolExecutionAuthorization(
            id=value.id, tenant_id=value.tenant_id, task_id=value.task_id,
            governed_action_id=value.governed_action_id, principal_id=value.principal_id,
            server_id=value.server_id, server_version_id=value.server_version_id,
            configuration_digest=value.configuration_digest, tool_key=value.tool_key,
            tool_name=value.tool_name, side_effect=ToolSideEffect(value.side_effect),
            schema_digest=value.schema_digest, arguments_digest=value.arguments_digest,
            idempotency_key_digest=value.idempotency_key_digest,
            status=ToolAuthorizationStatus(value.status), invocation_id=value.invocation_id,
            created_at=value.created_at, completed_at=value.completed_at,
        )
