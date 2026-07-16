from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
                        raise ToolInvocationFailed(
                            "MCP Tool published an invalid input schema"
                        ) from exc
                    except ValidationError as exc:
                        raise InvalidToolRequest(
                            f"Tool arguments do not match the published schema: {exc.message}"
                        ) from exc

                    response = await session.call_tool(
                        binding.tool_name,
                        arguments,
                        read_timeout_seconds=self._timeout,
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
                    if len(encoded) > self._max_result_bytes:
                        raise ToolResultTooLarge(len(encoded), self._max_result_bytes)
                    return ToolCallResult(
                        output=output,
                        protocol_version=str(initialized.protocolVersion),
                        schema_digest=canonical_json_digest(schema),
                        result_digest=canonical_json_digest(output),
                        result_bytes=len(encoded),
                    )
