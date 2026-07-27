from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentmesh.application.ports import (
    AgentExecutionContext,
    AgentExecutor,
    SecretValueProvider,
    UnitOfWorkFactory,
)
from agentmesh.domain.credentials import SecretPurpose, SecretReferenceStatus
from agentmesh.domain.model_runtime import ModelRuntimePolicy
from agentmesh.domain.pricing import UsagePriceCatalog
from agentmesh.domain.registry import AgentVersion
from agentmesh.orchestration.mcp_agent import GovernedModelToolRuntime, ModelToolSession

OPENAI_API_AUDIENCE = "https://api.openai.com"


class ModelProviderError(RuntimeError):
    """A bounded provider call failed or returned an invalid response."""


class ResponsesTransport(Protocol):
    def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class OpenAIResponsesTransport:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int,
        max_request_bytes: int,
        max_response_bytes: int,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("OpenAI API key must not be blank")
        self._api_key = normalized_key
        self._timeout_seconds = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > self._max_request_bytes:
            raise ModelProviderError("Model request exceeds the configured byte limit")
        request = Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise ModelProviderError(f"OpenAI Responses API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise ModelProviderError("OpenAI Responses API is unavailable") from exc
        if len(raw) > self._max_response_bytes:
            raise ModelProviderError("Model response exceeds the configured byte limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProviderError("Model response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ModelProviderError("Model response must be a JSON object")
        return value


class OpenAIResponsesAgentExecutor:
    def __init__(
        self,
        *,
        transport: ResponsesTransport,
        model: str,
        reasoning_effort: str,
        max_output_tokens: int,
        max_context_bytes: int = 131_072,
        price_catalog: UsagePriceCatalog | None = None,
        tool_runtime: GovernedModelToolRuntime | None = None,
    ) -> None:
        self._transport = transport
        self._model = model.strip()
        self._reasoning_effort = reasoning_effort.strip().lower()
        self._max_output_tokens = max_output_tokens
        self._max_context_bytes = max_context_bytes
        self._price_catalog = price_catalog
        self._tool_runtime = tool_runtime

    def execute_version(
        self,
        *,
        version: AgentVersion,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": self._user_input(objective, input)}],
            }
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": version.instructions,
            "input": input_items,
            "reasoning": {"effort": self._reasoning_effort},
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "safety_identifier": sha256(context.tenant_id.encode()).hexdigest(),
        }
        session = self._tool_runtime.open_session(version) if self._tool_runtime else None
        if session is not None:
            payload["tools"] = list(session.definitions)
        response, tool_calls = self._run_loop(
            payload=payload,
            input_items=input_items,
            session=session,
            context=context,
        )
        text = self._output_text(response)
        return {
            "summary": text,
            "agent": {
                "id": context.agent_id,
                "version_id": str(version.id),
                "version_digest": version.content_digest,
                "role": version.role,
                "kind": "openai-responses",
                "model": self._model,
            },
            "execution": {
                "task_id": str(context.task_id),
                "run_id": str(context.run_id),
                "thread_id": context.thread_id,
                "response_id": response.get("id"),
                "model_tool_calls": tool_calls,
            },
        }

    def _run_loop(
        self,
        *,
        payload: dict[str, Any],
        input_items: list[dict[str, Any]],
        session: ModelToolSession | None,
        context: AgentExecutionContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        calls_used = 0
        audit: list[dict[str, Any]] = []
        while True:
            response = self._transport.create(payload)
            self._report_usage(response, context)
            output = response.get("output")
            if not isinstance(output, list):
                raise ModelProviderError("Model response output must be a list")
            function_calls = [
                item
                for item in output
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if not function_calls:
                return response, audit
            if session is None or self._tool_runtime is None:
                raise ModelProviderError("Model requested a Tool when no Tool session is active")
            if calls_used + len(function_calls) > session.max_calls:
                raise ModelProviderError("Model exceeded the Agent Tool call budget")

            # store=false requires replaying complete provider output, including reasoning items.
            input_items.extend(item for item in output if isinstance(item, dict))
            for call in function_calls:
                name = call.get("name")
                call_id = call.get("call_id")
                raw_arguments = call.get("arguments")
                if not all(isinstance(value, str) and value for value in (name, call_id)):
                    raise ModelProviderError("Model function_call identity is invalid")
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ModelProviderError(
                        "Model function_call arguments are invalid JSON"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelProviderError("Model function_call arguments must be an object")
                result = self._tool_runtime.invoke(
                    session=session,
                    model_name=name,
                    arguments=arguments,
                    context=context,
                )
                calls_used += 1
                audit.append(
                    {
                        "call_id": call_id,
                        "invocation_id": result.invocation_id,
                        "tool": result.tool_key,
                        "server": result.server_name,
                        "schema_digest": result.schema_digest,
                    }
                )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result.output, ensure_ascii=False, sort_keys=True),
                    }
                )
            payload["input"] = input_items

    def _report_usage(self, response: dict[str, Any], context: AgentExecutionContext) -> None:
        usage = response.get("usage")
        if isinstance(usage, dict):
            buckets = {
                key: value
                for key, value in usage.items()
                if key in {"input_tokens", "output_tokens", "total_tokens"}
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            if buckets:
                quote = (
                    self._price_catalog.quote(
                        provider="openai", model=self._model, usage=buckets
                    )
                    if self._price_catalog
                    else None
                )
                context.report_usage(
                    provider="openai",
                    model=self._model,
                    usage_details=buckets,
                    cost_details_micros=quote.cost_details_micros if quote else None,
                    currency=quote.currency if quote else "USD",
                    pricing_version=quote.pricing_version if quote else None,
                )

    def _user_input(self, objective: str, input: dict[str, Any]) -> str:
        serialized = json.dumps(input, ensure_ascii=False, sort_keys=True)
        encoded = serialized.encode()
        if len(encoded) > self._max_context_bytes:
            preview_budget = max(256, self._max_context_bytes - 512)
            preview = encoded[:preview_budget].decode("utf-8", errors="ignore")
            serialized = json.dumps(
                {
                    "_agentmesh_context": {
                        "compacted": True,
                        "original_bytes": len(encoded),
                        "sha256": sha256(encoded).hexdigest(),
                        "preview": preview,
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return (
            f"Objective:\n{objective}\n\n"
            "Available structured context:\n"
            f"{serialized}\n\n"
            "Return a concise but complete result. Preserve material evidence and caveats."
        )

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        values: list[str] = []
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        values.append(part["text"])
        text = "\n".join(value.strip() for value in values if value.strip()).strip()
        if not text:
            raise ModelProviderError("Model response contains no output text")
        return text


class VersionBoundAgentExecutor:
    """Resolve the immutable Agent Version before choosing a runtime executor."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        fallback: AgentExecutor,
        model_executor: OpenAIResponsesAgentExecutor | None = None,
        default_policy: ModelRuntimePolicy | None = None,
        default_api_key: str | None = None,
        transport_factory: Callable[[str], ResponsesTransport] | None = None,
        secret_provider: SecretValueProvider | None = None,
        tool_runtime: GovernedModelToolRuntime | None = None,
        max_context_bytes: int = 131_072,
        price_catalog: UsagePriceCatalog | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._fallback = fallback
        self._model_executor = model_executor
        self._default_policy = default_policy or ModelRuntimePolicy(
            "deterministic", None, None, None, None
        )
        self._default_api_key = default_api_key
        self._transport_factory = transport_factory
        self._secret_provider = secret_provider
        self._tool_runtime = tool_runtime
        self._max_context_bytes = max_context_bytes
        self._price_catalog = price_catalog

    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        if context.agent_version_id is None or context.agent_version_digest is None:
            raise ModelProviderError("Run has no immutable Agent Version binding")
        with self._uow_factory() as uow:
            version = uow.agent_versions.get(context.agent_version_id)
        if version is None or version.content_digest != context.agent_version_digest:
            raise ModelProviderError("Run Agent Version binding no longer matches the Registry")
        policy = ModelRuntimePolicy.from_dict(version.model_policy)
        if policy.provider == "inherit" and self._model_executor is not None:
            executor = self._model_executor
        else:
            resolved = self._default_policy if policy.provider == "inherit" else policy
            if resolved.provider == "deterministic":
                executor = None
            elif resolved.provider == "openai":
                executor = self._openai_executor(resolved, context)
            else:
                raise ModelProviderError(f"Unsupported model provider '{resolved.provider}'")
        if executor is not None:
            return executor.execute_version(
                version=version,
                objective=objective,
                input=input,
                context=context,
            )
        output = self._fallback.execute(objective=objective, input=input, context=context)
        agent = output.get("agent")
        if isinstance(agent, dict):
            agent["role"] = version.role
        return output

    def _openai_executor(
        self, policy: ModelRuntimePolicy, context: AgentExecutionContext
    ) -> OpenAIResponsesAgentExecutor:
        if self._transport_factory is None:
            raise ModelProviderError("OpenAI transport factory is not configured")
        api_key = self._default_api_key
        if policy.credential_reference_id is not None:
            if self._secret_provider is None:
                raise ModelProviderError("Model credential provider is not configured")
            with self._uow_factory() as uow:
                reference = uow.credentials.get_secret_reference(policy.credential_reference_id)
            if reference is None:
                raise ModelProviderError("Agent model credential reference does not exist")
            if reference.tenant_id != context.tenant_id:
                raise ModelProviderError("Agent model credential belongs to another tenant")
            if reference.status is not SecretReferenceStatus.ACTIVE:
                raise ModelProviderError("Agent model credential reference is revoked")
            if reference.purpose is not SecretPurpose.MODEL_PROVIDER_API_KEY:
                raise ModelProviderError("Agent model credential purpose is invalid")
            if OPENAI_API_AUDIENCE not in reference.allowed_audiences:
                raise ModelProviderError("Agent model credential does not allow OpenAI API access")
            api_key = self._secret_provider.resolve(reference)
        if api_key is None or not api_key.strip():
            raise ModelProviderError("Agent model has no available OpenAI credential")
        assert policy.model is not None
        assert policy.reasoning_effort is not None
        assert policy.max_output_tokens is not None
        return OpenAIResponsesAgentExecutor(
            transport=self._transport_factory(api_key),
            model=policy.model,
            reasoning_effort=policy.reasoning_effort,
            max_output_tokens=policy.max_output_tokens,
            tool_runtime=self._tool_runtime,
            max_context_bytes=self._max_context_bytes,
            price_catalog=self._price_catalog,
        )
