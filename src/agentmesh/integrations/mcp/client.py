from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
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
from agentmesh.application.ports import ReadOnlyToolGateway
from agentmesh.domain.errors import (
    AgentMeshError,
    InvalidToolRequest,
    ToolInvocationFailed,
    ToolOutcomeUnknown,
    ToolResultTooLarge,
)
from agentmesh.domain.mcp_registry import McpCapabilityDiscovery, McpDiscoveredTool
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
    """Pinned Streamable HTTP client for reads and authorized idempotent writes."""

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
        if binding.side_effect not in {
            ToolSideEffect.READ_ONLY,
            ToolSideEffect.IDEMPOTENT_WRITE,
        }:
            raise InvalidToolRequest("MCP Gateway rejected an unsafe Tool binding")
        if binding.side_effect is ToolSideEffect.IDEMPOTENT_WRITE:
            key = arguments.get("idempotency_key")
            if (
                not isinstance(key, str)
                or not key
                or key != key.strip()
                or len(key) > 255
            ):
                raise InvalidToolRequest("MCP write requires a bounded idempotency_key")
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
                    self._invoke_with_retry_async(
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
                if isinstance(known_error, ToolOutcomeUnknown):
                    used = True
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

    async def _invoke_with_retry_async(self, **kwargs) -> ToolCallResult:
        binding = kwargs["binding"]
        attempts = 2 if binding.side_effect is ToolSideEffect.IDEMPOTENT_WRITE else 1
        for attempt in range(attempts):
            try:
                return await self._invoke_async(**kwargs)
            except Exception as exc:
                known_error = StdioMcpReadOnlyToolGateway._find_agentmesh_error(exc)
                if not isinstance(known_error, ToolOutcomeUnknown):
                    raise
                if attempt + 1 >= attempts:
                    raise known_error from exc
        raise AssertionError("unreachable")

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


class CircuitBreakingMcpToolGateway:
    """Process-local fail-fast guard; durable invocation evidence remains in PostgreSQL."""

    def __init__(
        self,
        delegate: ReadOnlyToolGateway,
        *,
        failure_threshold: int = 5,
        recovery_seconds: int = 30,
        clock=time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_seconds < 1:
            raise ValueError("MCP circuit limits must be positive")
        self._delegate = delegate
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._state: dict[str, tuple[int, float | None]] = {}
        self._lock = threading.Lock()

    def invoke(self, **kwargs) -> ToolCallResult:
        binding = kwargs["binding"]
        key = f"{binding.server_id}:{binding.server_version_id}"
        now = self._clock()
        with self._lock:
            failures, opened_at = self._state.get(key, (0, None))
            if opened_at is not None and now - opened_at < self._recovery_seconds:
                raise ToolInvocationFailed("MCP Server circuit is open")
        try:
            result = self._delegate.invoke(**kwargs)
        except Exception:
            with self._lock:
                failures += 1
                self._state[key] = (
                    failures,
                    now if failures >= self._failure_threshold else None,
                )
            raise
        with self._lock:
            self._state.pop(key, None)
        return result


class StreamableHttpMcpDiscoveryGateway:
    """Performs bounded public discovery without publishing or widening Catalog bindings."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        max_tools: int,
        resolver: Resolver = socket.getaddrinfo,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if max_tools < 1 or max_tools > 4096:
            raise ValueError("MCP discovery max_tools must be between 1 and 4096")
        self._guard = StreamableHttpMcpReadOnlyToolGateway(
            timeout_seconds=timeout_seconds,
            max_result_bytes=max_response_bytes,
            resolver=resolver,
            ssl_context=ssl_context,
        )
        self._max_tools = max_tools
        self._max_discovery_bytes = max_response_bytes

    def discover(
        self,
        *,
        endpoint_reference: str,
        expected_server_name: str,
        expected_protocol_version: str,
    ) -> McpCapabilityDiscovery:
        binding = ToolBinding(
            logical_key="discovery.probe",
            server_name=expected_server_name,
            tool_name="discovery.probe",
            side_effect=ToolSideEffect.READ_ONLY,
            server_id=UUID(int=0),
            server_version_id=UUID(int=0),
            transport="STREAMABLE_HTTP",
            endpoint_reference=endpoint_reference,
            protocol_version=expected_protocol_version,
            configuration_digest="sha256:" + "0" * 64,
        )
        endpoint, host, port = self._guard._target(binding)
        addresses = self._guard._public_addresses(host, port)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(
                    self._discover_async(
                        endpoint=endpoint,
                        host=host,
                        pinned_address=addresses[0],
                        expected_server_name=expected_server_name,
                        expected_protocol_version=expected_protocol_version,
                    )
                )
            except Exception as exc:
                known_error = StdioMcpReadOnlyToolGateway._find_agentmesh_error(exc)
                if known_error is not None:
                    raise known_error from exc
                raise ToolInvocationFailed(
                    f"MCP discovery transport failed: {type(exc).__name__}"
                ) from exc
        raise ToolInvocationFailed("Synchronous MCP discovery cannot run inside an event loop")

    async def _discover_async(
        self,
        *,
        endpoint: str,
        host: str,
        pinned_address: str,
        expected_server_name: str,
        expected_protocol_version: str,
    ) -> McpCapabilityDiscovery:
        backend = _PinnedNetworkBackend(expected_host=host, pinned_address=pinned_address)
        pool = httpcore.AsyncConnectionPool(
            ssl_context=self._guard._ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )
        transport = _BoundedAsyncHTTPTransport(
            max_response_bytes=self._guard._max_result_bytes,
            verify=self._guard._ssl_context,
            trust_env=False,
            retries=0,
        )
        transport._pool = pool
        async with httpx.AsyncClient(
            transport=transport,
            timeout=self._guard._timeout.total_seconds(),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(
                endpoint, http_client=client, terminate_on_close=True
            ) as (read, write, _session_id):
                async with ClientSession(
                    read, write, read_timeout_seconds=self._guard._timeout
                ) as session:
                    initialized = await session.initialize()
                    protocol = str(initialized.protocolVersion)
                    server_name = initialized.serverInfo.name
                    if protocol != expected_protocol_version:
                        raise ToolInvocationFailed(
                            "MCP discovery negotiated a different protocol version"
                        )
                    if server_name != expected_server_name:
                        raise ToolInvocationFailed("MCP discovery Server identity mismatch")
                    discovered = await self._collect_tools(session)
                    return McpCapabilityDiscovery(
                        server_name=server_name,
                        protocol_version=protocol,
                        tools=discovered,
                    )

    async def _collect_tools(self, session) -> tuple[McpDiscoveredTool, ...]:
        discovered: list[McpDiscoveredTool] = []
        discovered_bytes = 0
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                schema = dict(tool.inputSchema)
                try:
                    Draft202012Validator.check_schema(schema)
                except SchemaError as exc:
                    raise ToolInvocationFailed(
                        "MCP discovery returned an invalid Tool schema"
                    ) from exc
                annotation = tool.annotations
                discovered_bytes += len(tool.name.encode("utf-8")) + len(
                    json.dumps(
                        schema,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
                if discovered_bytes > self._max_discovery_bytes:
                    raise ToolInvocationFailed(
                        "MCP discovery exceeded the aggregate metadata byte limit"
                    )
                discovered.append(
                    McpDiscoveredTool.create(
                        name=tool.name,
                        input_schema=schema,
                        read_only_hint=(
                            annotation.readOnlyHint if annotation is not None else None
                        ),
                        idempotent_hint=(
                            getattr(annotation, "idempotentHint", None)
                            if annotation is not None
                            else None
                        ),
                    )
                )
                if len(discovered) > self._max_tools:
                    raise ToolInvocationFailed(
                        "MCP discovery exceeded the configured Tool count limit"
                    )
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise ToolInvocationFailed("MCP discovery returned a cursor cycle")
            seen_cursors.add(cursor)
        names = [tool.name for tool in discovered]
        if len(names) != len(set(names)):
            raise ToolInvocationFailed("MCP discovery returned duplicate Tool names")
        return tuple(sorted(discovered, key=lambda value: value.name))


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
    if binding.side_effect is ToolSideEffect.READ_ONLY:
        if tool.annotations is None or tool.annotations.readOnlyHint is not True:
            raise ToolInvocationFailed("MCP Tool does not declare readOnlyHint=true")
    elif binding.side_effect is ToolSideEffect.IDEMPOTENT_WRITE:
        if (
            tool.annotations is None
            or tool.annotations.readOnlyHint is True
            or tool.annotations.idempotentHint is not True
        ):
            raise ToolInvocationFailed(
                "MCP write Tool no longer declares idempotentHint=true"
            )
        key = arguments.get("idempotency_key")
        if (
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or len(key) > 255
        ):
            raise InvalidToolRequest("MCP write requires a bounded idempotency_key")
    else:
        raise InvalidToolRequest("MCP Gateway rejected an unsafe Tool binding")
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
    try:
        response = await session.call_tool(
            binding.tool_name,
            arguments,
            read_timeout_seconds=timeout,
            meta={
                "io.agentmesh/invocation-id": str(invocation_id),
                "io.agentmesh/idempotency-key-digest": canonical_json_digest(
                    arguments.get("idempotency_key")
                )
                if binding.side_effect is ToolSideEffect.IDEMPOTENT_WRITE
                else None,
            },
        )
    except Exception as exc:
        if binding.side_effect is ToolSideEffect.IDEMPOTENT_WRITE:
            raise ToolOutcomeUnknown(
                "MCP write delivery outcome could not be confirmed"
            ) from exc
        raise
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
