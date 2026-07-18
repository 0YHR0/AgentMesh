from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidMcpRegistry,
    McpRegistryConflict,
    McpRegistryNotFound,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.mcp_registry import (
    McpServer,
    McpServerStatus,
    McpServerVersion,
    McpServerVersionStatus,
    McpToolCapability,
    McpTransport,
)
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.policy import GovernedActionType
from agentmesh.domain.tools import ToolBinding, ToolSideEffect


@dataclass(frozen=True)
class McpServerView:
    server: McpServer
    versions: tuple[tuple[McpServerVersion, tuple[McpToolCapability, ...]], ...]


class McpRegistryService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        policy_service: PolicyApprovalService,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._policy = policy_service

    def ensure_builtin_workspace(
        self,
        *,
        server_name: str,
        tool_name: str,
        logical_key: str,
        input_schema: dict[str, Any],
    ) -> ToolBinding:
        with self._uow_factory() as uow:
            server = uow.mcp_registry.get_server_by_name(
                tenant_id=self._tenant_id, name=server_name
            )
            if server is None:
                server = McpServer.create(
                    tenant_id=self._tenant_id,
                    owner_id="agentmesh-system",
                    name=server_name,
                    description="Bundled confined read-only workspace MCP Server.",
                    transport=McpTransport.MANAGED_STDIO,
                    endpoint_reference="builtin://workspace",
                )
                uow.mcp_registry.add_server(server)
                version = McpServerVersion.create(
                    tenant_id=self._tenant_id,
                    server_id=server.id,
                    semantic_version="1.0.0",
                    protocol_version="2025-11-25",
                    configuration={"adapter": "builtin-workspace", "tool": tool_name},
                )
                tool = McpToolCapability.create(
                    tenant_id=self._tenant_id,
                    server_version_id=version.id,
                    logical_key=logical_key,
                    tool_name=tool_name,
                    description="Read one UTF-8 text file below the configured workspace root.",
                    side_effect=ToolSideEffect.READ_ONLY,
                    input_schema=input_schema,
                )
                uow.mcp_registry.add_version(version.publish())
                uow.mcp_registry.add_tool(tool)
                uow.mcp_registry.save_server(server.activate())
                self._event(uow, server.id, "agentmesh.mcp.builtin-seeded", "agentmesh-system")
                uow.commit()
        return self.resolve(logical_key)

    def register_server(
        self,
        *,
        owner_id: str,
        name: str,
        description: str,
        transport: McpTransport,
        endpoint_reference: str,
        actor: str,
        idempotency_key: str,
    ) -> McpServer:
        request = {
            "owner_id": owner_id.strip(),
            "name": name.strip(),
            "description": description.strip(),
            "transport": transport.value,
            "endpoint_reference": endpoint_reference.strip(),
        }
        request_hash = _digest(request)
        scope = f"mcp-server:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                server = uow.mcp_registry.get_server(UUID(replay["server_id"]))
                if server is None:
                    raise McpRegistryConflict("MCP Server idempotency result was lost")
                return server
            if uow.mcp_registry.get_server_by_name(tenant_id=self._tenant_id, name=request["name"]):
                raise McpRegistryConflict("MCP Server name already exists")
            server = McpServer.create(
                tenant_id=self._tenant_id,
                owner_id=owner_id,
                name=name,
                description=description,
                transport=transport,
                endpoint_reference=endpoint_reference,
            )
            uow.mcp_registry.add_server(server)
            self._record(uow, scope, idempotency_key, request_hash, {"server_id": str(server.id)})
            self._event(uow, server.id, "agentmesh.mcp.server-registered", actor)
            uow.commit()
            return server

    def add_version(
        self,
        server_id: UUID,
        *,
        semantic_version: str,
        protocol_version: str,
        configuration: dict[str, Any],
        actor: str,
        idempotency_key: str,
    ) -> McpServerVersion:
        candidate = McpServerVersion.create(
            tenant_id=self._tenant_id,
            server_id=server_id,
            semantic_version=semantic_version,
            protocol_version=protocol_version,
            configuration=configuration,
        )
        request_hash = _digest(
            {
                "server_id": str(server_id),
                "semantic_version": candidate.semantic_version,
                "protocol_version": candidate.protocol_version,
                "configuration_digest": candidate.configuration_digest,
            }
        )
        scope = f"mcp-version:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            self._owned_server(uow, server_id)
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                version = uow.mcp_registry.get_version(UUID(replay["version_id"]))
                if version is None:
                    raise McpRegistryConflict("MCP Version idempotency result was lost")
                return version
            existing = uow.mcp_registry.get_version_by_semantic(
                server_id, candidate.semantic_version
            )
            if existing is not None:
                raise McpRegistryConflict("MCP Server semantic version already exists")
            uow.mcp_registry.add_version(candidate)
            self._record(
                uow, scope, idempotency_key, request_hash, {"version_id": str(candidate.id)}
            )
            self._event(uow, candidate.id, "agentmesh.mcp.version-created", actor)
            uow.commit()
            return candidate

    def add_tool(
        self,
        version_id: UUID,
        *,
        logical_key: str,
        tool_name: str,
        description: str,
        side_effect: ToolSideEffect,
        input_schema: dict[str, Any],
        actor: str,
        idempotency_key: str,
    ) -> McpToolCapability:
        try:
            Draft202012Validator.check_schema(input_schema)
        except SchemaError as exc:
            raise InvalidMcpRegistry("MCP Tool input_schema is not valid JSON Schema") from exc
        candidate = McpToolCapability.create(
            tenant_id=self._tenant_id,
            server_version_id=version_id,
            logical_key=logical_key,
            tool_name=tool_name,
            description=description,
            side_effect=side_effect,
            input_schema=input_schema,
        )
        request_hash = _digest(
            {
                "version_id": str(version_id),
                "logical_key": candidate.logical_key,
                "tool_name": candidate.tool_name,
                "description": candidate.description,
                "side_effect": candidate.side_effect.value,
                "schema_digest": candidate.schema_digest,
            }
        )
        scope = f"mcp-tool:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            version = self._owned_version(uow, version_id)
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                existing = next(
                    (
                        tool
                        for tool in uow.mcp_registry.list_tools(version_id)
                        if str(tool.id) == replay["tool_id"]
                    ),
                    None,
                )
                if existing is None:
                    raise McpRegistryConflict("MCP Tool idempotency result was lost")
                return existing
            if version.status is not McpServerVersionStatus.DRAFT:
                raise McpRegistryConflict("Published MCP Version snapshots are immutable")
            if any(
                tool.logical_key == candidate.logical_key
                for tool in uow.mcp_registry.list_tools(version_id)
            ):
                raise McpRegistryConflict("Logical Tool key already exists in this Version")
            uow.mcp_registry.add_tool(candidate)
            self._record(uow, scope, idempotency_key, request_hash, {"tool_id": str(candidate.id)})
            self._event(uow, candidate.id, "agentmesh.mcp.tool-declared", actor)
            uow.commit()
            return candidate

    def publish_version(
        self,
        version_id: UUID,
        *,
        principal: PrincipalContext,
        permit_id: UUID | None,
    ) -> McpServerVersion:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise InvalidMcpRegistry("MCP publication requires an authenticated tenant Principal")
        with self._uow_factory() as uow:
            version = self._owned_version(uow, version_id)
            server = self._owned_server(uow, version.server_id)
            tools = uow.mcp_registry.list_tools(version.id)
        if version.status is McpServerVersionStatus.PUBLISHED:
            return version
        if not tools:
            raise InvalidMcpRegistry("MCP Server Version requires at least one Tool snapshot")
        arguments = self.policy_arguments(version, tools)
        if any(tool.side_effect.requires_approval for tool in tools):
            if not self._policy.enabled:
                raise InvalidMcpRegistry("Write-capable MCP publication requires Policy")
            self._policy.consume_permit(
                permit_id,
                principal=principal,
                action_type=GovernedActionType.MCP_SERVER_VERSION_PUBLISH,
                resource_type="mcp_server_version",
                resource_id=version.id,
                arguments=arguments,
            )
        with self._uow_factory() as uow:
            current = self._owned_version(uow, version.id, for_update=True)
            current_server = self._owned_server(uow, server.id, for_update=True)
            current_tools = uow.mcp_registry.list_tools(current.id)
            if self.policy_arguments(current, current_tools) != arguments:
                raise McpRegistryConflict(
                    "MCP Version snapshot changed during publication; request a new approval"
                )
            for logical_key in sorted(tool.logical_key for tool in current_tools):
                uow.mcp_registry.lock_catalog_key(
                    tenant_id=self._tenant_id, logical_key=logical_key
                )
            self._assert_no_published_key_conflict(uow, current, current_tools)
            published = current.publish()
            active = current_server.activate()
            uow.mcp_registry.save_version(published)
            uow.mcp_registry.save_server(active)
            self._event(
                uow, published.id, "agentmesh.mcp.version-published", principal.principal_id
            )
            uow.commit()
            return published

    def revoke_version(self, version_id: UUID, *, reason: str, actor: str) -> McpServerVersion:
        with self._uow_factory() as uow:
            version = self._owned_version(uow, version_id, for_update=True)
            updated = version.revoke(reason)
            if updated is version:
                return version
            uow.mcp_registry.save_version(updated)
            self._event(uow, updated.id, "agentmesh.mcp.version-revoked", actor)
            uow.commit()
            return updated

    def suspend_server(self, server_id: UUID, *, actor: str) -> McpServer:
        with self._uow_factory() as uow:
            server = self._owned_server(uow, server_id, for_update=True)
            updated = server.suspend()
            if updated is server:
                return server
            uow.mcp_registry.save_server(updated)
            self._event(uow, updated.id, "agentmesh.mcp.server-suspended", actor)
            uow.commit()
            return updated

    def list_servers(self, *, limit: int, offset: int) -> list[McpServerView]:
        with self._uow_factory() as uow:
            servers = uow.mcp_registry.list_servers(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )
            return [
                McpServerView(
                    server=server,
                    versions=tuple(
                        (version, tuple(uow.mcp_registry.list_tools(version.id)))
                        for version in uow.mcp_registry.list_versions(server.id)
                    ),
                )
                for server in servers
            ]

    def resolve(self, logical_key: str) -> ToolBinding:
        with self._uow_factory() as uow:
            matches = []
            for tool in uow.mcp_registry.list_tools_by_key(
                tenant_id=self._tenant_id, logical_key=logical_key.strip()
            ):
                version = uow.mcp_registry.get_version(tool.server_version_id)
                if version is None or version.status is not McpServerVersionStatus.PUBLISHED:
                    continue
                server = uow.mcp_registry.get_server(version.server_id)
                if server is None or server.status is not McpServerStatus.ACTIVE:
                    continue
                matches.append((server, version, tool))
        if not matches:
            raise InvalidMcpRegistry(f"Tool '{logical_key}' is not published in the MCP Catalog")
        if len(matches) != 1:
            raise McpRegistryConflict(f"Tool '{logical_key}' has ambiguous published bindings")
        server, version, tool = matches[0]
        return ToolBinding(
            logical_key=tool.logical_key,
            server_name=server.name,
            tool_name=tool.tool_name,
            side_effect=tool.side_effect,
            server_version_id=version.id,
            schema_digest=tool.schema_digest,
        )

    @staticmethod
    def policy_arguments(
        version: McpServerVersion, tools: list[McpToolCapability]
    ) -> dict[str, Any]:
        return {
            "configuration_digest": version.configuration_digest,
            "tools": [
                {
                    "logical_key": tool.logical_key,
                    "tool_name": tool.tool_name,
                    "schema_digest": tool.schema_digest,
                    "side_effect": tool.side_effect.value,
                }
                for tool in sorted(tools, key=lambda value: value.logical_key)
            ],
        }

    def _assert_no_published_key_conflict(
        self, uow, version: McpServerVersion, tools: list[McpToolCapability]
    ) -> None:
        for tool in tools:
            for existing in uow.mcp_registry.list_tools_by_key(
                tenant_id=self._tenant_id, logical_key=tool.logical_key
            ):
                if existing.server_version_id == version.id:
                    continue
                other = uow.mcp_registry.get_version(existing.server_version_id)
                if other is not None and other.status is McpServerVersionStatus.PUBLISHED:
                    raise McpRegistryConflict(
                        f"Tool '{tool.logical_key}' already has a published binding"
                    )

    def _owned_server(self, uow, server_id: UUID, *, for_update: bool = False) -> McpServer:
        server = uow.mcp_registry.get_server(server_id, for_update=for_update)
        if server is None or server.tenant_id != self._tenant_id:
            raise McpRegistryNotFound(f"MCP Server {server_id} was not found")
        return server

    def _owned_version(
        self, uow, version_id: UUID, *, for_update: bool = False
    ) -> McpServerVersion:
        version = uow.mcp_registry.get_version(version_id, for_update=for_update)
        if version is None or version.tenant_id != self._tenant_id:
            raise McpRegistryNotFound(f"MCP Server Version {version_id} was not found")
        return version

    @staticmethod
    def _replay(uow, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        normalized = key.strip()
        if not normalized:
            raise InvalidMcpRegistry("Idempotency-Key must not be blank")
        uow.idempotency.lock(scope, normalized)
        existing = uow.idempotency.get(scope, normalized)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency-Key was reused for another MCP request")
        return existing.result

    @staticmethod
    def _record(uow, scope: str, key: str, request_hash: str, result: dict[str, Any]) -> None:
        uow.idempotency.add(
            IdempotencyRecord.create(
                scope=scope,
                key=key.strip(),
                request_hash=request_hash,
                result=result,
            )
        )

    def _event(self, uow, aggregate_id: UUID, schema: str, actor: str) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema,
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"aggregate_id": str(aggregate_id), "actor": actor},
            )
        )


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
