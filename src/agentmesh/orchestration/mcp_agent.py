from __future__ import annotations

from typing import Any

from agentmesh.application.ports import AgentExecutionContext, AgentExecutor, ReadOnlyToolGateway
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.errors import InvalidToolRequest, ToolInvocationFailed
from agentmesh.domain.tools import ToolBinding, ToolCallRequest
from agentmesh.features import Feature, FeatureGateSet


class ReadOnlyMcpAgentExecutor:
    """Routes an explicit Task tool_call through one pinned read-only MCP binding."""

    def __init__(
        self,
        *,
        fallback: AgentExecutor,
        feature_gates: FeatureGateSet,
        binding: ToolBinding,
        gateway: ReadOnlyToolGateway | None,
        invocation_service: ToolInvocationService | None,
    ) -> None:
        self._fallback = fallback
        self._feature_gates = feature_gates
        self._binding = binding
        self._gateway = gateway
        self._invocation_service = invocation_service

    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        request = ToolCallRequest.from_task_input(input)
        if request is None:
            return self._fallback.execute(objective=objective, input=input, context=context)

        self._feature_gates.require(Feature.MCP_READ_TOOLS)
        if request.tool_key != self._binding.logical_key:
            raise InvalidToolRequest(f"Tool '{request.tool_key}' is not in the current allowlist")
        if self._gateway is None or self._invocation_service is None:
            raise ToolInvocationFailed("MCP read-only Tool runtime is not configured")

        invocation = self._invocation_service.start(
            task_id=context.task_id,
            run_id=context.run_id,
            binding=self._binding,
            arguments=request.arguments,
        )
        try:
            result = self._gateway.invoke(
                invocation_id=invocation.id,
                binding=self._binding,
                arguments=request.arguments,
            )
        except Exception as exc:
            self._invocation_service.fail(
                invocation.id,
                f"MCP invocation failed: {type(exc).__name__}",
            )
            raise

        completed = self._invocation_service.succeed(invocation.id, result)
        output = self._fallback.execute(objective=objective, input=input, context=context)
        output["tool_invocation"] = {
            "id": str(completed.id),
            "tool": completed.tool_key,
            "server": completed.server_name,
            "side_effect": completed.side_effect.value,
            "schema_digest": completed.schema_digest,
            "result": result.output,
        }
        return output
