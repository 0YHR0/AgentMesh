from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from agentmesh.application.ports import (
    AgentExecutionContext,
    AgentExecutor,
    ReadOnlyToolGateway,
    ToolCatalog,
)
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.errors import InvalidToolRequest, ToolInvocationFailed, ToolOutcomeUnknown
from agentmesh.domain.model_runtime import AgentToolPolicy
from agentmesh.domain.registry import AgentVersion
from agentmesh.domain.tools import ToolBinding, ToolCallRequest, ToolSideEffect
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class ModelToolResult:
    output: dict[str, Any]
    invocation_id: str
    tool_key: str
    server_name: str
    schema_digest: str | None


@dataclass(frozen=True)
class ModelToolSession:
    definitions: tuple[dict[str, Any], ...]
    names: dict[str, str]
    max_calls: int


class GovernedModelToolRuntime:
    """Expose a digest-bound, read-only Catalog subset to one model execution."""

    def __init__(
        self,
        *,
        feature_gates: FeatureGateSet,
        gateway: ReadOnlyToolGateway | None,
        invocation_service: ToolInvocationService | None,
        catalog: ToolCatalog | None,
    ) -> None:
        self._feature_gates = feature_gates
        self._gateway = gateway
        self._invocation_service = invocation_service
        self._catalog = catalog

    def open_session(self, version: AgentVersion) -> ModelToolSession | None:
        policy = AgentToolPolicy.from_dict(version.tool_profile)
        if not policy.allowed_tools:
            return None
        self._feature_gates.require(Feature.MODEL_TOOL_LOOP)
        if self._catalog is None:
            raise ToolInvocationFailed("Governed MCP Catalog is not configured")
        definitions: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for logical_key in policy.allowed_tools:
            binding = self._catalog.resolve(logical_key)
            self._require_read_only(binding)
            if not isinstance(binding.input_schema, dict):
                raise ToolInvocationFailed(f"Tool '{logical_key}' has no pinned input schema")
            model_name = self._model_name(logical_key)
            names[model_name] = logical_key
            definitions.append(
                {
                    "type": "function",
                    "name": model_name,
                    "description": binding.description or f"Invoke {logical_key}",
                    "parameters": dict(binding.input_schema),
                    "strict": True,
                }
            )
        return ModelToolSession(tuple(definitions), names, policy.max_calls)

    def invoke(
        self,
        *,
        session: ModelToolSession,
        model_name: str,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> ModelToolResult:
        logical_key = session.names.get(model_name)
        if logical_key is None:
            raise InvalidToolRequest(f"Model Tool '{model_name}' is not in the Agent allowlist")
        if self._catalog is None or self._gateway is None or self._invocation_service is None:
            raise ToolInvocationFailed("Governed model Tool runtime is not configured")
        binding = self._catalog.resolve(logical_key)
        self._require_read_only(binding)
        completed, output = _invoke_binding(
            binding=binding,
            arguments=arguments,
            gateway=self._gateway,
            invocation_service=self._invocation_service,
            context=context,
        )
        return ModelToolResult(
            output=output,
            invocation_id=str(completed.id),
            tool_key=completed.tool_key,
            server_name=completed.server_name,
            schema_digest=completed.schema_digest,
        )

    @staticmethod
    def _require_read_only(binding: ToolBinding) -> None:
        if binding.side_effect is not ToolSideEffect.READ_ONLY:
            raise InvalidToolRequest(
                f"Model-originated Tool '{binding.logical_key}' must be read-only"
            )

    @staticmethod
    def _model_name(logical_key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", logical_key)[:40].strip("_") or "tool"
        digest = sha256(logical_key.encode()).hexdigest()[:10]
        return f"agentmesh_{safe}_{digest}"


def _invoke_binding(
    *,
    binding: ToolBinding,
    arguments: dict[str, Any],
    gateway: ReadOnlyToolGateway,
    invocation_service: ToolInvocationService,
    context: AgentExecutionContext,
):
    invocation = invocation_service.start(
        task_id=context.task_id,
        run_id=context.run_id,
        binding=binding,
        arguments=arguments,
    )
    try:
        result = gateway.invoke(
            invocation_id=invocation.id,
            task_id=context.task_id,
            run_id=context.run_id,
            binding=binding,
            arguments=arguments,
        )
    except ToolOutcomeUnknown as exc:
        invocation_service.outcome_unknown(
            invocation.id,
            f"MCP write outcome unknown: {type(exc).__name__}",
        )
        raise
    except Exception as exc:
        invocation_service.fail(
            invocation.id,
            f"MCP invocation failed: {type(exc).__name__}",
        )
        raise
    completed = invocation_service.succeed(invocation.id, result)
    return completed, result.output


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
        catalog: ToolCatalog | None = None,
    ) -> None:
        self._fallback = fallback
        self._feature_gates = feature_gates
        self._binding = binding
        self._gateway = gateway
        self._invocation_service = invocation_service
        self._catalog = catalog

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
        binding = self._binding
        if self._feature_gates.is_enabled(Feature.GOVERNED_MCP):
            if self._catalog is None:
                raise ToolInvocationFailed("Governed MCP Catalog is not configured")
            binding = self._catalog.resolve(request.tool_key)
        if request.tool_key != binding.logical_key:
            raise InvalidToolRequest(f"Tool '{request.tool_key}' is not in the current allowlist")
        if self._gateway is None or self._invocation_service is None:
            raise ToolInvocationFailed("MCP read-only Tool runtime is not configured")
        if binding.side_effect is not ToolSideEffect.READ_ONLY:
            self._feature_gates.require(Feature.MCP_WRITE_TOOLS)

        completed, result_output = _invoke_binding(
            binding=binding,
            arguments=request.arguments,
            gateway=self._gateway,
            invocation_service=self._invocation_service,
            context=context,
        )
        output = self._fallback.execute(objective=objective, input=input, context=context)
        output["tool_invocation"] = {
            "id": str(completed.id),
            "tool": completed.tool_key,
            "server": completed.server_name,
            "side_effect": completed.side_effect.value,
            "schema_digest": completed.schema_digest,
            "result": result_output,
        }
        return output
