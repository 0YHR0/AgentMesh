from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from agentmesh.application.model_connection_services import ModelConnectionService
from agentmesh.application.ports import (
    AgentExecutionContext,
    AgentExecutor,
    IncompleteAgentOutput,
    SecretValueProvider,
    UnitOfWorkFactory,
)
from agentmesh.domain.credentials import SecretPurpose, SecretReferenceStatus
from agentmesh.domain.model_connections import validate_endpoint
from agentmesh.domain.model_runtime import ModelRuntimePolicy
from agentmesh.domain.pricing import UsagePriceCatalog
from agentmesh.domain.registry import AgentVersion
from agentmesh.orchestration.mcp_agent import GovernedModelToolRuntime, ModelToolSession

OPENAI_API_AUDIENCE = "https://api.openai.com"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_opener():
    return build_opener(HTTPSHandler(), _NoRedirect())


def urlopen(request: Request, timeout: int):
    """Compatibility seam for tests; production requests reject every redirect."""
    return _https_opener().open(request, timeout=timeout)


class ModelProviderError(RuntimeError):
    """A bounded provider call failed or returned an invalid response."""


class ModelOutputTruncated(ModelProviderError, IncompleteAgentOutput):
    """The provider explicitly reported that the output token limit cut off its reply."""


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
            with urlopen(request, timeout=self._timeout_seconds) as response:
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


class DeepSeekChatCompletionsTransport:
    """Bounded non-streaming Chat Completions adapter with tool-call normalization."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        timeout_seconds: int,
        max_request_bytes: int,
        max_response_bytes: int,
    ) -> None:
        self._api_key = api_key.strip()
        if not self._api_key:
            raise ValueError("DeepSeek API key must not be blank")
        self._endpoint = validate_endpoint("deepseek", endpoint)
        self._timeout_seconds = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        pending_calls: list[dict[str, Any]] = []

        def flush_calls() -> None:
            if pending_calls:
                messages.append({"role": "assistant", "tool_calls": list(pending_calls)})
                pending_calls.clear()

        for item in payload.get("input", []):
            if not isinstance(item, dict):
                continue
            if item.get("role") == "user":
                flush_calls()
                content = item.get("content", [])
                text = (
                    "\n".join(
                        p["text"]
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "input_text"
                    )
                    if isinstance(content, list)
                    else str(content)
                )
                messages.append({"role": "user", "content": text})
            elif item.get("role") == "assistant":
                flush_calls()
                content = item.get("content", [])
                text = (
                    "\n".join(
                        p["text"]
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "output_text"
                    )
                    if isinstance(content, list)
                    else str(content)
                )
                if text:
                    messages.append({"role": "assistant", "content": text})
            elif item.get("type") == "function_call":
                pending_calls.append(
                    {
                        "id": item.get("call_id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                )
            elif item.get("type") == "function_call_output":
                flush_calls()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.get("call_id"),
                        "content": item.get("output", ""),
                    }
                )
        flush_calls()
        # DeepSeek Chat Completions uses system messages instead of Responses instructions.
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions:
            messages.insert(0, {"role": "system", "content": instructions})
        request_payload: dict[str, Any] = {
            "model": payload["model"],
            "messages": messages,
            "max_tokens": payload.get("max_output_tokens", 1200),
            "stream": False,
            # Keep the Chat Completions adapter on the non-thinking tool-call path. The
            # Responses runner does not carry provider reasoning_content between turns.
            "thinking": {"type": "disabled"},
        }
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            request_payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "description": item.get("description", ""),
                        "parameters": item.get("parameters", {"type": "object", "properties": {}}),
                        "strict": item.get("strict", True),
                    },
                }
                for item in tools
            ]
            request_payload["tool_choice"] = "auto"
        body = json.dumps(request_payload, separators=(",", ":")).encode()
        if len(body) > self._max_request_bytes:
            raise ModelProviderError("Model request exceeds the configured byte limit")
        request = Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise ModelProviderError(
                f"DeepSeek Chat Completions API returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise ModelProviderError("DeepSeek Chat Completions API is unavailable") from exc
        if len(raw) > self._max_response_bytes:
            raise ModelProviderError("Model response exceeds the configured byte limit")
        try:
            result = json.loads(raw)
            choices = result.get("choices") if isinstance(result, dict) else None
            first_choice = (
                choices[0]
                if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                else None
            )
            choice = first_choice.get("message") if first_choice is not None else None
            if not isinstance(choice, dict):
                raise ValueError("missing completion message")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise ModelProviderError("DeepSeek response is invalid") from exc
        output: list[dict[str, Any]] = []
        if isinstance(choice.get("content"), str) and choice["content"]:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": choice["content"]}],
                }
            )
        calls = choice.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    continue
                function = call["function"]
                output.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments", "{}"),
                    }
                )
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return {
            "id": result.get("id"),
            "output": output,
            "finish_reason": first_choice.get("finish_reason"),
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }


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
        provider: str = "openai",
    ) -> None:
        self._transport = transport
        self._model = model.strip()
        self._reasoning_effort = reasoning_effort.strip().lower()
        self._max_output_tokens = max_output_tokens
        self._max_context_bytes = max_context_bytes
        self._price_catalog = price_catalog
        self._tool_runtime = tool_runtime
        self._provider = provider

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
        structured = self._structured_result(
            text, acceptance_contract=input.get("agentmesh_deliverable_contract")
        )
        return {
            **structured,
            "agent": {
                "id": context.agent_id,
                "version_id": str(version.id),
                "version_digest": version.content_digest,
                "role": version.role,
                "kind": "openai-responses"
                if self._provider == "openai"
                else "deepseek-chat-completions",
                "model": self._model,
            },
            "execution": {
                "task_id": str(context.task_id),
                "run_id": str(context.run_id),
                "thread_id": context.thread_id,
                "response_id": response.get("id"),
                "model_finish_reason": response.get("finish_reason") or response.get("status"),
                "model_tool_calls": tool_calls,
            },
        }

    @staticmethod
    def _structured_result(
        text: str, acceptance_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preserve normal text and explicitly governed structured output contracts."""
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {"summary": text}
        if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
            return {"summary": text}
        result: dict[str, Any] = {"summary": value["summary"]}
        if (
            isinstance(acceptance_contract, dict)
            and type(acceptance_contract.get("version")) is int
            and acceptance_contract["version"] == 1
        ):
            roots = acceptance_contract.get("output_roots")
            if isinstance(roots, list):
                for root in roots[:60]:
                    if (
                        isinstance(root, str)
                        and root not in {"agent", "execution", "memory_candidates"}
                        and root in value
                    ):
                        result[root] = value[root]
        candidates = value.get("memory_candidates")
        if isinstance(candidates, list):
            result["memory_candidates"] = candidates
        research_deliverable = value.get("research_deliverable")
        if isinstance(research_deliverable, dict):
            result["research_deliverable"] = research_deliverable
        return result

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
            self._require_complete_response(response)
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
                        "output": json.dumps(
                            {
                                **result.output,
                                "_agentmesh_tool_evidence": {
                                    "invocation_id": result.invocation_id,
                                    "tool": result.tool_key,
                                    "server": result.server_name,
                                    "schema_digest": result.schema_digest,
                                },
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )
            payload["input"] = input_items

    @staticmethod
    def _require_complete_response(response: dict[str, Any]) -> None:
        status = response.get("status")
        if status is not None and status != "completed":
            if status == "incomplete":
                details = response.get("incomplete_details")
                if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                    raise ModelOutputTruncated(
                        "Model output was truncated at the configured token limit"
                    )
                raise ModelProviderError("Model output was incomplete; revise the work item")
            raise ModelProviderError("Model response did not complete")
        finish_reason = response.get("finish_reason")
        if finish_reason == "length":
            raise ModelOutputTruncated("Model output was truncated at the configured token limit")
        if finish_reason is not None and finish_reason not in {"stop", "tool_calls"}:
            raise ModelProviderError("Model response did not finish normally")

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
                        provider=self._provider, model=self._model, usage=buckets
                    )
                    if self._price_catalog
                    else None
                )
                context.report_usage(
                    provider=self._provider,
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
        model_connection_service: ModelConnectionService | None = None,
        model_timeout_seconds: int = 120,
        max_request_bytes: int = 262_144,
        max_response_bytes: int = 1_048_576,
        identity_rbac_enabled: bool = False,
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
        self._model_connection_service = model_connection_service
        self._model_timeout_seconds = model_timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._identity_rbac_enabled = identity_rbac_enabled

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
            elif resolved.provider in {"openai", "deepseek"}:
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
        endpoint = "https://api.openai.com/v1/responses"
        if policy.connection_id is not None:
            if not self._identity_rbac_enabled:
                raise ModelProviderError("Identity RBAC must be enabled for model connections")
            if self._model_connection_service is None or policy.connection_snapshot is None:
                raise ModelProviderError("Published model connection snapshot is unavailable")
            with self._uow_factory() as uow:
                connection = uow.model_connections.get(context.tenant_id, policy.connection_id)
            if connection is None or not connection.enabled:
                raise ModelProviderError("Published model connection is disabled or unavailable")
            snapshot = policy.connection_snapshot
            if self._model_connection_service is None:
                raise ModelProviderError("Model connection credential provider is unavailable")
            if (
                snapshot.get("provider") != policy.provider
                or snapshot.get("model") != policy.model
                or connection.provider != snapshot.get("provider")
            ):
                raise ModelProviderError(
                    "Published model connection snapshot does not match its policy"
                )
            endpoint = snapshot["endpoint"]
            api_key = self._model_connection_service.resolve_secret(connection)
            if policy.provider == "deepseek":
                transport: ResponsesTransport = DeepSeekChatCompletionsTransport(
                    api_key=api_key,
                    endpoint=endpoint,
                    timeout_seconds=self._model_timeout_seconds,
                    max_request_bytes=self._max_request_bytes,
                    max_response_bytes=self._max_response_bytes,
                )
            else:
                transport = (
                    self._transport_factory(api_key)
                    if self._transport_factory
                    else OpenAIResponsesTransport(
                        api_key=api_key,
                        timeout_seconds=self._model_timeout_seconds,
                        max_request_bytes=self._max_request_bytes,
                        max_response_bytes=self._max_response_bytes,
                    )
                )
            assert policy.model is not None
            return OpenAIResponsesAgentExecutor(
                transport=transport,
                model=policy.model,
                reasoning_effort=policy.reasoning_effort or "none",
                max_output_tokens=policy.max_output_tokens or 1200,
                tool_runtime=self._tool_runtime,
                max_context_bytes=self._max_context_bytes,
                price_catalog=self._price_catalog,
                provider=policy.provider,
            )
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
