from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
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
