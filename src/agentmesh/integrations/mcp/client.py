from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpcore
import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.domain.errors import (
    AgentMeshError,
    InvalidToolRequest,
    ToolInvocationFailed,
    ToolResultTooLarge,
)
from agentmesh.domain.tools import (
    ToolBinding,
    ToolCallResult,
    ToolSideEffect,
    canonical_json_digest,
)

Resolver = Any


class StdioMcpReadOnlyToolGateway:
    def __init__(
        self,
        *,
        command: str,
        arguments: list[str],
        environment: dict[str, str],
        working_directory: str | Path | None,
        timeout_seconds: int,
        max_result_bytes: int,
    ) -> None:
        if not command.strip() or timeout_seconds < 1 or max_result_bytes < 1:
            raise ValueError("MCP command, timeout, and result limit must be valid")
        self._server = StdioServerParameters(
            command=command,
            args=list(arguments),
            env=dict(environment),
            cwd=working_directory,
        )
        self._timeout = timedelta(seconds=timeout_seconds)
        self._max_result_bytes = max_result_bytes

    def invoke(
        self,
        *,
        invocation_id: UUID,
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        if binding.side_effect is not ToolSideEffect.READ_ONLY:
            raise InvalidToolRequest("MCP Gateway rejected a non-read-only Tool binding")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(
                    self._invoke_async(
                        invocation_id=invocation_id,
                        binding=binding,
                        arguments=arguments,
                    )
                )
            except Exception as exc:
                known_error = self._find_agentmesh_error(exc)
                if known_error is not None:
                    raise known_error from exc
                raise ToolInvocationFailed(f"MCP transport failed: {type(exc).__name__}") from exc
        raise ToolInvocationFailed("Synchronous MCP Gateway cannot run inside an event loop")

    @classmethod
    def _find_agentmesh_error(cls, error: BaseException) -> AgentMeshError | None:
        if isinstance(error, AgentMeshError):
            return error
        for nested in getattr(error, "exceptions", ()):
            known_error = cls._find_agentmesh_error(nested)
            if known_error is not None:
                return known_error
        return None

    async def _invoke_async(
        self,
        *,
        invocation_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        with open(os.devnull, "w", encoding="utf-8") as error_log:
            async with stdio_client(self._server, errlog=error_log) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self._timeout,
                ) as session:
                    initialized = await session.initialize()
                    return await _call_bound_tool(
                        session=session,
                        initialized=initialized,
                        invocation_id=invocation_id,
                        binding=binding,
                        arguments=arguments,
                        timeout=self._timeout,
                        max_result_bytes=self._max_result_bytes,
                    )


class StreamableHttpMcpReadOnlyToolGateway:
    """Pinned, no-redirect Streamable HTTP MCP client for published read-only bindings."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_result_bytes: int,
        credential_broker: CredentialBrokerService | None = None,
        workload_principal_id: UUID | None = None,
        resolver: Resolver = socket.getaddrinfo,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if timeout_seconds < 1 or max_result_bytes < 1:
            raise ValueError("MCP HTTP timeout and result limit must be positive")
        self._timeout = timedelta(seconds=timeout_seconds)
        self._max_result_bytes = max_result_bytes
        self._credential_broker = credential_broker
        self._workload_principal_id = workload_principal_id
        self._resolver = resolver
        if ssl_context is None:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True
        self._ssl_context = ssl_context

    def invoke(
        self,
        *,
        invocation_id: UUID,
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        if binding.side_effect is not ToolSideEffect.READ_ONLY:
            raise InvalidToolRequest("MCP Gateway rejected a non-read-only Tool binding")
        endpoint, host, port = self._target(binding)
        addresses = self._public_addresses(host, port)
        grant = None
        if binding.authentication_required and (
            self._credential_broker is None or self._workload_principal_id is None
        ):
            raise ToolInvocationFailed(
                "MCP Server requires the Credential Broker and a workload Principal"
            )
        if self._credential_broker is not None and self._workload_principal_id is not None:
            assert binding.server_id is not None
            assert binding.server_version_id is not None
            assert binding.configuration_digest is not None
            grant = self._credential_broker.acquire_for_mcp(
                workload_principal_id=self._workload_principal_id,
                server_id=binding.server_id,
                server_version_id=binding.server_version_id,
                configuration_digest=binding.configuration_digest,
                audience=endpoint,
                authentication_required=binding.authentication_required,
                tool_invocation_id=invocation_id,
                task_id=task_id,
                run_id=run_id,
            )
        used = False
        settlement_error = "mcp_invocation_failed"
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(
                    self._invoke_async(
                        endpoint=endpoint,
                        host=host,
                        pinned_address=addresses[0],
                        invocation_id=invocation_id,
                        binding=binding,
                        arguments=arguments,
                        credential=grant.material if grant else None,
                    )
                )
                used = True
                return result
            raise ToolInvocationFailed("Synchronous MCP Gateway cannot run inside an event loop")
        except Exception as exc:
            settlement_error = type(exc).__name__
            known_error = StdioMcpReadOnlyToolGateway._find_agentmesh_error(exc)
            if known_error is not None:
                raise known_error from exc
            raise ToolInvocationFailed(
                f"MCP Streamable HTTP transport failed: {type(exc).__name__}"
            ) from exc
        finally:
            if grant is not None:
                assert self._credential_broker is not None
                self._credential_broker.settle_mcp_lease(
                    grant.lease.id,
                    used=used,
                    error=None if used else settlement_error,
                )

    def _target(self, binding: ToolBinding) -> tuple[str, str, int]:
        if (
            binding.transport != "STREAMABLE_HTTP"
            or binding.server_id is None
            or binding.server_version_id is None
            or binding.configuration_digest is None
            or binding.protocol_version != "2025-11-25"
            or binding.endpoint_reference is None
        ):
            raise ToolInvocationFailed("MCP Streamable HTTP binding is incomplete or unsupported")
        parsed = urlsplit(binding.endpoint_reference)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ToolInvocationFailed("MCP Streamable HTTP endpoint is not allowed")
        return (
            binding.endpoint_reference.rstrip("/"),
            parsed.hostname.lower().rstrip("."),
            (parsed.port or 443),
        )

    def _public_addresses(self, host: str, port: int) -> list[str]:
        try:
            records = self._resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ToolInvocationFailed("MCP endpoint DNS resolution failed") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ToolInvocationFailed(
                "MCP Streamable HTTP endpoint must resolve exclusively to public IP addresses"
            )
        return addresses

    async def _invoke_async(
        self,
        *,
        endpoint: str,
        host: str,
        pinned_address: str,
        invocation_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
        credential,
    ) -> ToolCallResult:
        headers = {}
        if credential is not None:
            if (
                credential.auth_scheme.lower() != "bearer"
                or not credential.value
                or "\r" in credential.value
                or "\n" in credential.value
                or len(credential.value) > 16_384
            ):
                raise ToolInvocationFailed("MCP credential material is invalid")
            headers["Authorization"] = f"Bearer {credential.value}"
        backend = _PinnedNetworkBackend(
            expected_host=host,
            pinned_address=pinned_address,
        )
        pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )
        transport = _BoundedAsyncHTTPTransport(
            max_response_bytes=self._max_result_bytes,
            verify=self._ssl_context,
            trust_env=False,
            retries=0,
        )
        transport._pool = pool
        async with httpx.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=self._timeout.total_seconds(),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(
                endpoint,
                http_client=client,
                terminate_on_close=True,
            ) as (read, write, _session_id):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self._timeout,
                ) as session:
                    initialized = await session.initialize()
                    if str(initialized.protocolVersion) != binding.protocol_version:
                        raise ToolInvocationFailed(
                            "MCP Server negotiated a different protocol version"
                        )
                    return await _call_bound_tool(
                        session=session,
                        initialized=initialized,
                        invocation_id=invocation_id,
                        binding=binding,
                        arguments=arguments,
                        timeout=self._timeout,
                        max_result_bytes=self._max_result_bytes,
                    )


class RoutedMcpReadOnlyToolGateway:
    def __init__(
        self,
        *,
        stdio: StdioMcpReadOnlyToolGateway,
        streamable_http: StreamableHttpMcpReadOnlyToolGateway,
    ) -> None:
        self._stdio = stdio
        self._streamable_http = streamable_http

    def invoke(self, **kwargs) -> ToolCallResult:
        binding = kwargs["binding"]
        if binding.transport == "MANAGED_STDIO":
            return self._stdio.invoke(**kwargs)
        if binding.transport == "STREAMABLE_HTTP":
            return self._streamable_http.invoke(**kwargs)
        raise ToolInvocationFailed("MCP Tool binding uses an unsupported transport")


class _PinnedNetworkBackend:
    def __init__(self, *, expected_host: str, pinned_address: str) -> None:
        self._expected_host = expected_host
        self._pinned_address = pinned_address
        self._delegate = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        normalized = host.decode() if isinstance(host, bytes) else host
        if normalized.lower().rstrip(".") != self._expected_host:
            raise OSError("MCP transport attempted an unapproved host")
        return await self._delegate.connect_tcp(
            self._pinned_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("MCP transport does not allow Unix sockets")

    async def sleep(self, seconds):
        await self._delegate.sleep(seconds)


class _BoundedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, max_response_bytes: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._max_response_bytes = max_response_bytes

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        response.stream = _BoundedAsyncByteStream(
            response.stream,
            self._max_response_bytes,
        )
        return response


class _BoundedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self):
        consumed = 0
        async for chunk in self._stream:
            consumed += len(chunk)
            if consumed > self._limit:
                raise ToolResultTooLarge(consumed, self._limit)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


async def _call_bound_tool(
    *,
    session,
    initialized,
    invocation_id: UUID,
    binding: ToolBinding,
    arguments: dict[str, Any],
    timeout: timedelta,
    max_result_bytes: int,
) -> ToolCallResult:
    if initialized.serverInfo.name != binding.server_name:
        raise ToolInvocationFailed("MCP Server identity does not match binding")
    tools = (await session.list_tools()).tools
    matches = [tool for tool in tools if tool.name == binding.tool_name]
    if len(matches) != 1:
        raise ToolInvocationFailed("Bound MCP Tool is unavailable or ambiguous")
    tool = matches[0]
    if tool.annotations is None or tool.annotations.readOnlyHint is not True:
        raise ToolInvocationFailed("MCP Tool does not declare readOnlyHint=true")
    schema = dict(tool.inputSchema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(arguments)
    except SchemaError as exc:
        raise ToolInvocationFailed("MCP Tool published an invalid input schema") from exc
    except ValidationError as exc:
        raise InvalidToolRequest(
            f"Tool arguments do not match the published schema: {exc.message}"
        ) from exc
    discovered_schema_digest = canonical_json_digest(schema)
    if binding.schema_digest is not None and binding.schema_digest != discovered_schema_digest:
        raise ToolInvocationFailed("MCP Tool schema changed from the published Registry snapshot")
    response = await session.call_tool(
        binding.tool_name,
        arguments,
        read_timeout_seconds=timeout,
        meta={"io.agentmesh/invocation-id": str(invocation_id)},
    )
    if response.isError:
        raise ToolInvocationFailed("MCP Tool returned an execution error")
    output: dict[str, Any] = {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in response.content
        ]
    }
    if response.structuredContent is not None:
        output["structured_content"] = dict(response.structuredContent)
    encoded = json.dumps(
        output,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > max_result_bytes:
        raise ToolResultTooLarge(len(encoded), max_result_bytes)
    return ToolCallResult(
        output=output,
        protocol_version=str(initialized.protocolVersion),
        schema_digest=discovered_schema_digest,
        result_digest=canonical_json_digest(output),
        result_bytes=len(encoded),
    )
