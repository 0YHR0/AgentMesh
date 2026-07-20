from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidMcpRegistry, InvalidMcpTransition
from agentmesh.domain.tasks import utc_now
from agentmesh.domain.tools import ToolSideEffect, canonical_json_digest


class McpTransport(str, Enum):
    MANAGED_STDIO = "MANAGED_STDIO"
    STREAMABLE_HTTP = "STREAMABLE_HTTP"


class McpServerStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class McpServerVersionStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    REVOKED = "REVOKED"


class McpDiscoveryStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    EXPANDED = "EXPANDED"
    INCOMPATIBLE = "INCOMPATIBLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class McpDiscoveredTool:
    name: str
    schema_digest: str
    read_only_hint: bool | None

    @classmethod
    def create(
        cls, *, name: str, input_schema: dict[str, Any], read_only_hint: bool | None
    ) -> McpDiscoveredTool:
        return cls(
            name=_bounded(name, "discovered tool name", 128),
            schema_digest=canonical_json_digest(input_schema),
            read_only_hint=read_only_hint,
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema_digest": self.schema_digest,
            "read_only_hint": self.read_only_hint,
        }


@dataclass(frozen=True)
class McpCapabilityDiscovery:
    server_name: str
    protocol_version: str
    tools: tuple[McpDiscoveredTool, ...]


@dataclass(frozen=True)
class McpDiscoverySnapshot:
    id: UUID
    tenant_id: str
    server_id: UUID
    server_version_id: UUID
    configuration_digest: str
    protocol_version: str
    server_name: str
    status: McpDiscoveryStatus
    capability_digest: str | None
    discovered_tools: tuple[McpDiscoveredTool, ...]
    error: str | None
    fetched_at: datetime
    expires_at: datetime
    created_by: str

    @classmethod
    def success(
        cls,
        *,
        tenant_id: str,
        server_id: UUID,
        server_version_id: UUID,
        configuration_digest: str,
        protocol_version: str,
        server_name: str,
        status: McpDiscoveryStatus,
        discovered_tools: tuple[McpDiscoveredTool, ...],
        ttl_seconds: int,
        created_by: str,
    ) -> McpDiscoverySnapshot:
        if status not in {
            McpDiscoveryStatus.COMPATIBLE,
            McpDiscoveryStatus.EXPANDED,
            McpDiscoveryStatus.INCOMPATIBLE,
        }:
            raise InvalidMcpRegistry("Successful discovery has an invalid status")
        if not 60 <= ttl_seconds <= 86_400:
            raise InvalidMcpRegistry("MCP discovery TTL must be between 60 and 86400 seconds")
        names = [tool.name for tool in discovered_tools]
        if len(names) != len(set(names)):
            raise InvalidMcpRegistry("MCP discovery returned duplicate Tool names")
        ordered = tuple(sorted(discovered_tools, key=lambda value: value.name))
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=_bounded(tenant_id, "tenant_id", 128),
            server_id=server_id,
            server_version_id=server_version_id,
            configuration_digest=configuration_digest,
            protocol_version=_bounded(protocol_version, "protocol_version", 32),
            server_name=_bounded(server_name, "server_name", 128),
            status=status,
            capability_digest=canonical_json_digest(
                [tool.canonical() for tool in ordered]
            ),
            discovered_tools=ordered,
            error=None,
            fetched_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            created_by=_bounded(created_by, "created_by", 128),
        )

    @classmethod
    def failed(
        cls,
        *,
        tenant_id: str,
        server_id: UUID,
        server_version_id: UUID,
        configuration_digest: str,
        protocol_version: str,
        server_name: str,
        ttl_seconds: int,
        created_by: str,
        error: str,
    ) -> McpDiscoverySnapshot:
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidMcpRegistry("Failed MCP discovery requires a safe error summary")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=_bounded(tenant_id, "tenant_id", 128),
            server_id=server_id,
            server_version_id=server_version_id,
            configuration_digest=configuration_digest,
            protocol_version=_bounded(protocol_version, "protocol_version", 32),
            server_name=_bounded(server_name, "server_name", 128),
            status=McpDiscoveryStatus.FAILED,
            capability_digest=None,
            discovered_tools=(),
            error=normalized_error[:2_000],
            fetched_at=now,
            expires_at=now + timedelta(seconds=max(60, min(ttl_seconds, 86_400))),
            created_by=_bounded(created_by, "created_by", 128),
        )

    def blocks_catalog(self, *, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return self.status in {
            McpDiscoveryStatus.INCOMPATIBLE,
            McpDiscoveryStatus.FAILED,
        } or self.expires_at <= current


def _bounded(value: str, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidMcpRegistry(f"{field} must contain 1-{maximum} characters")
    return normalized


@dataclass(frozen=True)
class McpServer:
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    description: str
    transport: McpTransport
    endpoint_reference: str
    status: McpServerStatus
    created_at: datetime
    updated_at: datetime
    revision: int = 1
    authentication_required: bool = False

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        description: str,
        transport: McpTransport,
        endpoint_reference: str,
        authentication_required: bool = False,
    ) -> McpServer:
        now = utc_now()
        endpoint = _bounded(endpoint_reference, "endpoint_reference", 512)
        if any(marker in endpoint.lower() for marker in ("token=", "password=", "secret=")):
            raise InvalidMcpRegistry("Endpoint reference must not contain credential material")
        if authentication_required and transport is not McpTransport.STREAMABLE_HTTP:
            raise InvalidMcpRegistry(
                "MCP transport authentication is supported only for Streamable HTTP"
            )
        if transport is McpTransport.STREAMABLE_HTTP:
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise InvalidMcpRegistry(
                    "Streamable HTTP endpoint_reference must be a bounded HTTPS URL"
                )
        return cls(
            id=uuid4(),
            tenant_id=_bounded(tenant_id, "tenant_id", 128),
            owner_id=_bounded(owner_id, "owner_id", 128),
            name=_bounded(name, "name", 128),
            description=description.strip()[:2_000],
            transport=transport,
            endpoint_reference=endpoint,
            authentication_required=authentication_required,
            status=McpServerStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )

    def activate(self) -> McpServer:
        if self.status is McpServerStatus.SUSPENDED:
            raise InvalidMcpTransition("A suspended MCP Server cannot be activated implicitly")
        if self.status is McpServerStatus.ACTIVE:
            return self
        return replace(
            self,
            status=McpServerStatus.ACTIVE,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def suspend(self) -> McpServer:
        if self.status is McpServerStatus.SUSPENDED:
            return self
        return replace(
            self,
            status=McpServerStatus.SUSPENDED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class McpServerVersion:
    id: UUID
    tenant_id: str
    server_id: UUID
    semantic_version: str
    protocol_version: str
    configuration_digest: str
    status: McpServerVersionStatus
    created_at: datetime
    published_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        server_id: UUID,
        semantic_version: str,
        protocol_version: str,
        configuration: dict[str, Any],
    ) -> McpServerVersion:
        return cls(
            id=uuid4(),
            tenant_id=_bounded(tenant_id, "tenant_id", 128),
            server_id=server_id,
            semantic_version=_bounded(semantic_version, "semantic_version", 64),
            protocol_version=_bounded(protocol_version, "protocol_version", 32),
            configuration_digest=canonical_json_digest(configuration),
            status=McpServerVersionStatus.DRAFT,
            created_at=utc_now(),
        )

    def publish(self) -> McpServerVersion:
        if self.status is not McpServerVersionStatus.DRAFT:
            raise InvalidMcpTransition("Only a draft MCP Server Version can be published")
        return replace(
            self,
            status=McpServerVersionStatus.PUBLISHED,
            published_at=utc_now(),
            revision=self.revision + 1,
        )

    def revoke(self, reason: str) -> McpServerVersion:
        if self.status is McpServerVersionStatus.REVOKED:
            return self
        if self.status is not McpServerVersionStatus.PUBLISHED or not reason.strip():
            raise InvalidMcpTransition("Published MCP Version revocation requires a reason")
        return replace(
            self,
            status=McpServerVersionStatus.REVOKED,
            revoked_at=utc_now(),
            revoke_reason=reason.strip()[:2_000],
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class McpToolCapability:
    id: UUID
    tenant_id: str
    server_version_id: UUID
    logical_key: str
    tool_name: str
    description: str
    side_effect: ToolSideEffect
    input_schema: dict[str, Any]
    schema_digest: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        server_version_id: UUID,
        logical_key: str,
        tool_name: str,
        description: str,
        side_effect: ToolSideEffect,
        input_schema: dict[str, Any],
    ) -> McpToolCapability:
        schema = dict(input_schema)
        return cls(
            id=uuid4(),
            tenant_id=_bounded(tenant_id, "tenant_id", 128),
            server_version_id=server_version_id,
            logical_key=_bounded(logical_key, "logical_key", 255),
            tool_name=_bounded(tool_name, "tool_name", 128),
            description=description.strip()[:2_000],
            side_effect=side_effect,
            input_schema=schema,
            schema_digest=canonical_json_digest(schema),
            created_at=utc_now(),
        )
