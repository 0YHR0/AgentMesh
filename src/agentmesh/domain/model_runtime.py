from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.domain.errors import InvalidAgentVersion

_MODEL_KEYS = {
    "provider",
    "model",
    "reasoning_effort",
    "max_output_tokens",
    "credential_reference_id",
}
_TOOL_KEYS = {"allowed_tools", "max_calls"}
_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class ModelRuntimePolicy:
    provider: str
    model: str | None
    reasoning_effort: str | None
    max_output_tokens: int | None
    credential_reference_id: UUID | None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ModelRuntimePolicy:
        if not value:
            return cls("inherit", None, None, None, None)
        unknown = set(value) - _MODEL_KEYS
        if unknown:
            raise InvalidAgentVersion("Unknown model_policy fields: " + ", ".join(sorted(unknown)))
        provider = value.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() not in {
            "deterministic",
            "openai",
        }:
            raise InvalidAgentVersion("model_policy.provider must be deterministic or openai")
        provider = provider.strip().lower()
        model = value.get("model")
        effort = value.get("reasoning_effort")
        max_tokens = value.get("max_output_tokens")
        reference = value.get("credential_reference_id")
        if provider == "deterministic":
            if any(item is not None for item in (model, effort, max_tokens, reference)):
                raise InvalidAgentVersion(
                    "Deterministic model_policy cannot include model or credential settings"
                )
            return cls(provider, None, None, None, None)
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 128:
            raise InvalidAgentVersion("OpenAI model_policy requires a bounded model name")
        if not isinstance(effort, str) or effort.strip().lower() not in _REASONING_EFFORTS:
            raise InvalidAgentVersion("OpenAI model_policy reasoning_effort is invalid")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 128 <= max_tokens <= 32_768
        ):
            raise InvalidAgentVersion(
                "OpenAI model_policy max_output_tokens must be between 128 and 32768"
            )
        reference_id = None
        if reference is not None:
            try:
                reference_id = UUID(str(reference))
            except (TypeError, ValueError) as exc:
                raise InvalidAgentVersion(
                    "model_policy credential_reference_id must be a UUID"
                ) from exc
        return cls(
            provider,
            model.strip(),
            effort.strip().lower(),
            max_tokens,
            reference_id,
        )

    def to_dict(self) -> dict[str, Any]:
        if self.provider == "inherit":
            return {}
        value: dict[str, Any] = {"provider": self.provider}
        if self.provider == "openai":
            value.update(
                {
                    "model": self.model,
                    "reasoning_effort": self.reasoning_effort,
                    "max_output_tokens": self.max_output_tokens,
                    "credential_reference_id": (
                        str(self.credential_reference_id)
                        if self.credential_reference_id is not None
                        else None
                    ),
                }
            )
        return value


@dataclass(frozen=True)
class AgentToolPolicy:
    allowed_tools: tuple[str, ...]
    max_calls: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentToolPolicy:
        if not value:
            return cls((), 0)
        unknown = set(value) - _TOOL_KEYS
        if unknown:
            raise InvalidAgentVersion("Unknown tool_profile fields: " + ", ".join(sorted(unknown)))
        raw_tools = value.get("allowed_tools")
        max_calls = value.get("max_calls")
        if not isinstance(raw_tools, list) or not raw_tools:
            raise InvalidAgentVersion("tool_profile.allowed_tools must be a non-empty list")
        tools: list[str] = []
        for raw in raw_tools:
            if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 255:
                raise InvalidAgentVersion("tool_profile contains an invalid Tool key")
            tools.append(raw.strip())
        normalized = tuple(sorted(set(tools)))
        if len(normalized) > 32:
            raise InvalidAgentVersion("Agent Version cannot allow more than 32 model Tools")
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or not 1 <= max_calls <= 8:
            raise InvalidAgentVersion("tool_profile.max_calls must be between 1 and 8")
        return cls(normalized, max_calls)

    def to_dict(self) -> dict[str, Any]:
        if not self.allowed_tools:
            return {}
        return {"allowed_tools": list(self.allowed_tools), "max_calls": self.max_calls}
