from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentmesh.application.ports import AgentExecutionContext, AgentExecutor, UnitOfWorkFactory
from agentmesh.domain.registry import AgentVersion


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
    ) -> None:
        self._transport = transport
        self._model = model.strip()
        self._reasoning_effort = reasoning_effort.strip().lower()
        self._max_output_tokens = max_output_tokens

    def execute_version(
        self,
        *,
        version: AgentVersion,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "instructions": version.instructions,
            "input": self._user_input(objective, input),
            "reasoning": {"effort": self._reasoning_effort},
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "safety_identifier": sha256(context.tenant_id.encode()).hexdigest(),
        }
        response = self._transport.create(payload)
        text = self._output_text(response)
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
                context.report_usage(provider="openai", model=self._model, usage_details=buckets)
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
            },
        }

    @staticmethod
    def _user_input(objective: str, input: dict[str, Any]) -> str:
        return (
            f"Objective:\n{objective}\n\n"
            "Available structured context:\n"
            f"{json.dumps(input, ensure_ascii=False, sort_keys=True)}\n\n"
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
    ) -> None:
        self._uow_factory = uow_factory
        self._fallback = fallback
        self._model_executor = model_executor

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
        if self._model_executor is not None:
            return self._model_executor.execute_version(
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
