import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidMcpRegistry, ToolInvocationFailed, ToolResultTooLarge
from agentmesh.domain.mcp_registry import McpServer, McpTransport
from agentmesh.domain.tools import ToolBinding, ToolCallResult, ToolSideEffect
from agentmesh.integrations.mcp.client import (
    CircuitBreakingMcpToolGateway,
    RoutedMcpReadOnlyToolGateway,
    StreamableHttpMcpDiscoveryGateway,
    StreamableHttpMcpReadOnlyToolGateway,
    _BoundedAsyncByteStream,
    _PinnedNetworkBackend,
)


def _binding(**changes) -> ToolBinding:
    values = {
        "logical_key": "docs.search",
        "server_name": "remote-docs",
        "tool_name": "search",
        "side_effect": ToolSideEffect.READ_ONLY,
        "server_id": uuid4(),
        "server_version_id": uuid4(),
        "schema_digest": "sha256:" + "1" * 64,
        "transport": "STREAMABLE_HTTP",
        "endpoint_reference": "https://mcp.example/mcp",
        "protocol_version": "2025-11-25",
        "configuration_digest": "sha256:" + "2" * 64,
        "authentication_required": False,
    }
    values.update(changes)
    return ToolBinding(**values)


def _result() -> ToolCallResult:
    return ToolCallResult(
        output={"content": []},
        protocol_version="2025-11-25",
        schema_digest="sha256:" + "1" * 64,
        result_digest="sha256:" + "3" * 64,
        result_bytes=14,
    )


def test_streamable_http_registration_requires_clean_https_endpoint() -> None:
    with pytest.raises(InvalidMcpRegistry, match="HTTPS URL"):
        McpServer.create(
            tenant_id="tenant",
            owner_id="owner",
            name="remote",
            description="",
            transport=McpTransport.STREAMABLE_HTTP,
            endpoint_reference="http://mcp.example/mcp",
        )


def test_streamable_http_gateway_rejects_any_private_dns_answer_before_invocation() -> None:
    gateway = StreamableHttpMcpReadOnlyToolGateway(
        timeout_seconds=5,
        max_result_bytes=4096,
        resolver=lambda *args, **kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ],
    )
    gateway._invoke_async = AsyncMock(return_value=_result())

    with pytest.raises(ToolInvocationFailed, match="public IP"):
        gateway.invoke(
            invocation_id=uuid4(),
            task_id=uuid4(),
            run_id=uuid4(),
            binding=_binding(),
            arguments={"query": "security"},
        )
    gateway._invoke_async.assert_not_called()


def test_streamable_http_gateway_pins_public_address_and_routes_result() -> None:
    gateway = StreamableHttpMcpReadOnlyToolGateway(
        timeout_seconds=5,
        max_result_bytes=4096,
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    gateway._invoke_async = AsyncMock(return_value=_result())
    invocation_id = uuid4()

    result = gateway.invoke(
        invocation_id=invocation_id,
        task_id=uuid4(),
        run_id=uuid4(),
        binding=_binding(),
        arguments={"query": "security"},
    )

    assert result == _result()
    call = gateway._invoke_async.await_args.kwargs
    assert call["endpoint"] == "https://mcp.example/mcp"
    assert call["host"] == "mcp.example"
    assert call["pinned_address"] == "93.184.216.34"
    assert call["invocation_id"] == invocation_id


def test_authentication_required_never_downgrades_without_broker() -> None:
    gateway = StreamableHttpMcpReadOnlyToolGateway(
        timeout_seconds=5,
        max_result_bytes=4096,
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    gateway._invoke_async = AsyncMock(return_value=_result())

    with pytest.raises(ToolInvocationFailed, match="requires the Credential Broker"):
        gateway.invoke(
            invocation_id=uuid4(),
            task_id=uuid4(),
            run_id=uuid4(),
            binding=_binding(authentication_required=True),
            arguments={},
        )
    gateway._invoke_async.assert_not_called()


def test_authenticated_invocation_failure_settles_lease_as_failed() -> None:
    class Broker:
        def __init__(self) -> None:
            self.lease_id = uuid4()
            self.settled = None

        def acquire_for_mcp(self, **kwargs):
            return SimpleNamespace(
                lease=SimpleNamespace(id=self.lease_id),
                material=SimpleNamespace(auth_scheme="Bearer", value="secret"),
            )

        def settle_mcp_lease(self, lease_id, **kwargs):
            self.settled = (lease_id, kwargs)

    broker = Broker()
    gateway = StreamableHttpMcpReadOnlyToolGateway(
        timeout_seconds=5,
        max_result_bytes=4096,
        credential_broker=broker,
        workload_principal_id=uuid4(),
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    gateway._invoke_async = AsyncMock(side_effect=ToolInvocationFailed("remote failed"))

    with pytest.raises(ToolInvocationFailed, match="remote failed"):
        gateway.invoke(
            invocation_id=uuid4(),
            task_id=uuid4(),
            run_id=uuid4(),
            binding=_binding(authentication_required=True),
            arguments={},
        )
    assert broker.settled == (
        broker.lease_id,
        {"used": False, "error": "ToolInvocationFailed"},
    )


def test_pinned_network_backend_ignores_runtime_dns_and_allows_only_bound_host() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.host = None

        async def connect_tcp(self, host, port, **kwargs):
            self.host = host
            return object()

        async def sleep(self, seconds):
            return None

    backend = _PinnedNetworkBackend(expected_host="mcp.example", pinned_address="93.184.216.34")
    delegate = Delegate()
    backend._delegate = delegate

    asyncio.run(backend.connect_tcp("mcp.example", 443))
    assert delegate.host == "93.184.216.34"
    with pytest.raises(OSError, match="unapproved host"):
        asyncio.run(backend.connect_tcp("redirect.example", 443))


def test_routed_gateway_selects_only_the_published_transport() -> None:
    class Gateway:
        def __init__(self, result) -> None:
            self.result = result
            self.calls = 0

        def invoke(self, **kwargs):
            self.calls += 1
            return self.result

    stdio = Gateway(_result())
    remote = Gateway(_result())
    routed = RoutedMcpReadOnlyToolGateway(stdio=stdio, streamable_http=remote)

    routed.invoke(
        invocation_id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        binding=_binding(),
        arguments={},
    )
    assert remote.calls == 1
    assert stdio.calls == 0


def test_mcp_circuit_fails_fast_and_recovers_after_cooldown() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0
            self.fail = True

        def invoke(self, **kwargs):
            self.calls += 1
            if self.fail:
                raise ToolInvocationFailed("remote unavailable")
            return _result()

    now = [10.0]
    delegate = Gateway()
    circuit = CircuitBreakingMcpToolGateway(
        delegate,
        failure_threshold=2,
        recovery_seconds=30,
        clock=lambda: now[0],
    )
    arguments = {
        "invocation_id": uuid4(),
        "task_id": uuid4(),
        "run_id": uuid4(),
        "binding": _binding(),
        "arguments": {},
    }

    with pytest.raises(ToolInvocationFailed, match="remote unavailable"):
        circuit.invoke(**arguments)
    with pytest.raises(ToolInvocationFailed, match="remote unavailable"):
        circuit.invoke(**arguments)
    with pytest.raises(ToolInvocationFailed, match="circuit is open"):
        circuit.invoke(**arguments)
    assert delegate.calls == 2

    now[0] += 31
    delegate.fail = False
    assert circuit.invoke(**arguments) == _result()
    assert delegate.calls == 3


def test_streamable_http_wire_response_is_bounded() -> None:
    class Stream:
        async def __aiter__(self):
            yield b"1234"
            yield b"5678"

        async def aclose(self):
            return None

    async def consume():
        return [chunk async for chunk in _BoundedAsyncByteStream(Stream(), 6)]

    with pytest.raises(ToolResultTooLarge):
        asyncio.run(consume())


def test_discovery_collects_paginated_tools_and_rejects_cursor_cycles() -> None:
    schema = {"type": "object"}

    class Session:
        def __init__(self, *, cycle: bool = False) -> None:
            self.cycle = cycle

        async def list_tools(self, cursor=None):
            if cursor is None:
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="zeta",
                            inputSchema=schema,
                            annotations=SimpleNamespace(readOnlyHint=True),
                        )
                    ],
                    nextCursor="next",
                )
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="alpha", inputSchema=schema, annotations=None)
                ],
                nextCursor="next" if self.cycle else None,
            )

    gateway = StreamableHttpMcpDiscoveryGateway(
        timeout_seconds=5,
        max_response_bytes=4096,
        max_tools=4,
    )
    tools = asyncio.run(gateway._collect_tools(Session()))
    assert [tool.name for tool in tools] == ["alpha", "zeta"]
    assert tools[0].read_only_hint is None
    assert tools[1].read_only_hint is True
    with pytest.raises(ToolInvocationFailed, match="cursor cycle"):
        asyncio.run(gateway._collect_tools(Session(cycle=True)))


def test_discovery_enforces_tool_count_limit() -> None:
    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="one", inputSchema={"type": "object"}, annotations=None
                    ),
                    SimpleNamespace(
                        name="two", inputSchema={"type": "object"}, annotations=None
                    ),
                ],
                nextCursor=None,
            )

    gateway = StreamableHttpMcpDiscoveryGateway(
        timeout_seconds=5,
        max_response_bytes=4096,
        max_tools=1,
    )
    with pytest.raises(ToolInvocationFailed, match="Tool count"):
        asyncio.run(gateway._collect_tools(Session()))

    byte_limited = StreamableHttpMcpDiscoveryGateway(
        timeout_seconds=5,
        max_response_bytes=8,
        max_tools=4,
    )
    with pytest.raises(ToolInvocationFailed, match="metadata byte limit"):
        asyncio.run(byte_limited._collect_tools(Session()))
