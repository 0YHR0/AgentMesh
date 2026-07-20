from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agentmesh.domain.mcp_registry import (
    McpServer,
    McpServerStatus,
    McpServerVersion,
    McpServerVersionStatus,
    McpToolCapability,
    McpTransport,
)
from agentmesh.domain.tools import ToolSideEffect
from agentmesh.infrastructure.postgres.models import (
    McpServerRecord,
    McpServerVersionRecord,
    McpToolCapabilityRecord,
)


class SqlAlchemyMcpRegistryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_catalog_key(self, *, tenant_id: str, logical_key: str) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:catalog_key, 0))"),
            {"catalog_key": f"{tenant_id}:{logical_key}"},
        )

    def add_server(self, server: McpServer) -> None:
        self._session.add(_server_record(server))

    def get_server(self, server_id: UUID, *, for_update: bool = False) -> McpServer | None:
        record = self._session.get(McpServerRecord, server_id, with_for_update=for_update)
        return _server(record) if record is not None else None

    def get_server_by_name(self, *, tenant_id: str, name: str) -> McpServer | None:
        record = self._session.scalar(
            select(McpServerRecord).where(
                McpServerRecord.tenant_id == tenant_id,
                McpServerRecord.name == name,
            )
        )
        return _server(record) if record is not None else None

    def save_server(self, server: McpServer) -> None:
        record = self._session.get(McpServerRecord, server.id)
        if record is None:
            raise LookupError(server.id)
        record.status = server.status.value
        record.updated_at = server.updated_at
        record.revision = server.revision

    def list_servers(self, *, tenant_id: str, limit: int, offset: int) -> list[McpServer]:
        records = self._session.scalars(
            select(McpServerRecord)
            .where(McpServerRecord.tenant_id == tenant_id)
            .order_by(McpServerRecord.created_at, McpServerRecord.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [_server(record) for record in records]

    def add_version(self, version: McpServerVersion) -> None:
        self._session.add(_version_record(version))

    def get_version(self, version_id: UUID, *, for_update: bool = False) -> McpServerVersion | None:
        record = self._session.get(McpServerVersionRecord, version_id, with_for_update=for_update)
        return _version(record) if record is not None else None

    def get_version_by_semantic(
        self, server_id: UUID, semantic_version: str
    ) -> McpServerVersion | None:
        record = self._session.scalar(
            select(McpServerVersionRecord).where(
                McpServerVersionRecord.server_id == server_id,
                McpServerVersionRecord.semantic_version == semantic_version,
            )
        )
        return _version(record) if record is not None else None

    def save_version(self, version: McpServerVersion) -> None:
        record = self._session.get(McpServerVersionRecord, version.id)
        if record is None:
            raise LookupError(version.id)
        record.status = version.status.value
        record.published_at = version.published_at
        record.revoked_at = version.revoked_at
        record.revoke_reason = version.revoke_reason
        record.revision = version.revision

    def list_versions(self, server_id: UUID) -> list[McpServerVersion]:
        records = self._session.scalars(
            select(McpServerVersionRecord)
            .where(McpServerVersionRecord.server_id == server_id)
            .order_by(McpServerVersionRecord.created_at, McpServerVersionRecord.id)
        ).all()
        return [_version(record) for record in records]

    def add_tool(self, tool: McpToolCapability) -> None:
        self._session.add(_tool_record(tool))

    def list_tools(self, server_version_id: UUID) -> list[McpToolCapability]:
        records = self._session.scalars(
            select(McpToolCapabilityRecord)
            .where(McpToolCapabilityRecord.server_version_id == server_version_id)
            .order_by(McpToolCapabilityRecord.logical_key)
        ).all()
        return [_tool(record) for record in records]

    def list_tools_by_key(self, *, tenant_id: str, logical_key: str) -> list[McpToolCapability]:
        records = self._session.scalars(
            select(McpToolCapabilityRecord)
            .where(
                McpToolCapabilityRecord.tenant_id == tenant_id,
                McpToolCapabilityRecord.logical_key == logical_key,
            )
            .order_by(McpToolCapabilityRecord.created_at.desc())
        ).all()
        return [_tool(record) for record in records]


def _server_record(value: McpServer) -> McpServerRecord:
    return McpServerRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        owner_id=value.owner_id,
        name=value.name,
        description=value.description,
        transport=value.transport.value,
        endpoint_reference=value.endpoint_reference,
        authentication_required=value.authentication_required,
        status=value.status.value,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _server(value: McpServerRecord) -> McpServer:
    return McpServer(
        id=value.id,
        tenant_id=value.tenant_id,
        owner_id=value.owner_id,
        name=value.name,
        description=value.description,
        transport=McpTransport(value.transport),
        endpoint_reference=value.endpoint_reference,
        authentication_required=value.authentication_required,
        status=McpServerStatus(value.status),
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _version_record(value: McpServerVersion) -> McpServerVersionRecord:
    return McpServerVersionRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        server_id=value.server_id,
        semantic_version=value.semantic_version,
        protocol_version=value.protocol_version,
        configuration_digest=value.configuration_digest,
        status=value.status.value,
        created_at=value.created_at,
        published_at=value.published_at,
        revoked_at=value.revoked_at,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
    )


def _version(value: McpServerVersionRecord) -> McpServerVersion:
    return McpServerVersion(
        id=value.id,
        tenant_id=value.tenant_id,
        server_id=value.server_id,
        semantic_version=value.semantic_version,
        protocol_version=value.protocol_version,
        configuration_digest=value.configuration_digest,
        status=McpServerVersionStatus(value.status),
        created_at=value.created_at,
        published_at=value.published_at,
        revoked_at=value.revoked_at,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
    )


def _tool_record(value: McpToolCapability) -> McpToolCapabilityRecord:
    return McpToolCapabilityRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        server_version_id=value.server_version_id,
        logical_key=value.logical_key,
        tool_name=value.tool_name,
        description=value.description,
        side_effect=value.side_effect.value,
        input_schema=dict(value.input_schema),
        schema_digest=value.schema_digest,
        created_at=value.created_at,
    )


def _tool(value: McpToolCapabilityRecord) -> McpToolCapability:
    return McpToolCapability(
        id=value.id,
        tenant_id=value.tenant_id,
        server_version_id=value.server_version_id,
        logical_key=value.logical_key,
        tool_name=value.tool_name,
        description=value.description,
        side_effect=ToolSideEffect(value.side_effect),
        input_schema=dict(value.input_schema),
        schema_digest=value.schema_digest,
        created_at=value.created_at,
    )
