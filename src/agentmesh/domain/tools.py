from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidToolRequest, ToolInvocationFailed
from agentmesh.domain.tasks import utc_now

WORKSPACE_READ_TOOL_KEY = "workspace.read_text"


class ToolSideEffect(str, Enum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
    IRREVERSIBLE = "IRREVERSIBLE"

    @property
    def requires_approval(self) -> bool:
        return self is not ToolSideEffect.READ_ONLY


class ToolInvocationStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def canonical_json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidToolRequest("Tool arguments must be JSON serializable") from exc
    return f"sha256:{sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class ToolCallRequest:
    tool_key: str
    arguments: dict[str, Any]

    @classmethod
    def from_task_input(cls, task_input: dict[str, Any]) -> ToolCallRequest | None:
        if "tool_call" not in task_input:
            return None
        raw = task_input["tool_call"]
        if not isinstance(raw, dict):
            raise InvalidToolRequest("tool_call must be a JSON object")
        if set(raw) != {"tool", "arguments"}:
            raise InvalidToolRequest("tool_call must contain only tool and arguments")
        tool_key = raw.get("tool")
        arguments = raw.get("arguments")
        if not isinstance(tool_key, str) or not tool_key.strip():
            raise InvalidToolRequest("tool_call.tool must be a non-empty string")
        if not isinstance(arguments, dict):
            raise InvalidToolRequest("tool_call.arguments must be a JSON object")
        canonical_json_digest(arguments)
        return cls(tool_key=tool_key.strip(), arguments=dict(arguments))


@dataclass(frozen=True)
class ToolBinding:
    logical_key: str
    server_name: str
    tool_name: str
    side_effect: ToolSideEffect
    server_id: UUID | None = None
    server_version_id: UUID | None = None
    schema_digest: str | None = None
    transport: str = "MANAGED_STDIO"
    endpoint_reference: str | None = None
    protocol_version: str | None = None
    configuration_digest: str | None = None
    authentication_required: bool = False


@dataclass(frozen=True)
class ToolCallResult:
    output: dict[str, Any]
    protocol_version: str
    schema_digest: str
    result_digest: str
    result_bytes: int


@dataclass
class ToolInvocation:
    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    server_name: str
    tool_key: str
    tool_name: str
    side_effect: ToolSideEffect
    protocol_version: str | None
    schema_digest: str | None
    arguments_digest: str
    status: ToolInvocationStatus
    result_digest: str | None
    result_bytes: int | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def start(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolInvocation:
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            server_name=binding.server_name,
            tool_key=binding.logical_key,
            tool_name=binding.tool_name,
            side_effect=binding.side_effect,
            protocol_version=None,
            schema_digest=None,
            arguments_digest=canonical_json_digest(arguments),
            status=ToolInvocationStatus.RUNNING,
            result_digest=None,
            result_bytes=None,
            error=None,
            started_at=utc_now(),
            completed_at=None,
        )

    def succeed(self, result: ToolCallResult) -> None:
        self._require_running("succeed")
        self.status = ToolInvocationStatus.SUCCEEDED
        self.protocol_version = result.protocol_version
        self.schema_digest = result.schema_digest
        self.result_digest = result.result_digest
        self.result_bytes = result.result_bytes
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        self._require_running("fail")
        normalized = error.strip()
        if not normalized:
            raise ToolInvocationFailed("Tool failure must include a safe error summary")
        self.status = ToolInvocationStatus.FAILED
        self.error = normalized[:2_000]
        self.completed_at = utc_now()

    def _require_running(self, action: str) -> None:
        if self.status is not ToolInvocationStatus.RUNNING:
            raise ToolInvocationFailed(
                f"Cannot {action} Tool Invocation {self.id} from {self.status.value}"
            )
