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
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ToolAuthorizationStatus(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


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

    def idempotency_key(self) -> str:
        value = self.arguments.get("idempotency_key")
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 255
        ):
            raise InvalidToolRequest(
                "Idempotent write Tool arguments require idempotency_key (1-255 characters)"
            )
        return value


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


@dataclass(frozen=True)
class ToolAuthorizationDraft:
    governed_action_id: UUID
    principal_id: str
    binding: ToolBinding
    arguments_digest: str
    idempotency_key_digest: str


@dataclass
class ToolExecutionAuthorization:
    id: UUID
    tenant_id: str
    task_id: UUID
    governed_action_id: UUID
    principal_id: str
    server_id: UUID
    server_version_id: UUID
    configuration_digest: str
    tool_key: str
    tool_name: str
    side_effect: ToolSideEffect
    schema_digest: str
    arguments_digest: str
    idempotency_key_digest: str
    status: ToolAuthorizationStatus
    invocation_id: UUID | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def create(
        cls, *, tenant_id: str, task_id: UUID, draft: ToolAuthorizationDraft
    ) -> ToolExecutionAuthorization:
        binding = draft.binding
        if (
            binding.side_effect is not ToolSideEffect.IDEMPOTENT_WRITE
            or binding.server_id is None
            or binding.server_version_id is None
            or binding.configuration_digest is None
            or binding.schema_digest is None
        ):
            raise InvalidToolRequest("MCP write authorization requires a pinned idempotent binding")
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            governed_action_id=draft.governed_action_id,
            principal_id=draft.principal_id,
            server_id=binding.server_id,
            server_version_id=binding.server_version_id,
            configuration_digest=binding.configuration_digest,
            tool_key=binding.logical_key,
            tool_name=binding.tool_name,
            side_effect=binding.side_effect,
            schema_digest=binding.schema_digest,
            arguments_digest=draft.arguments_digest,
            idempotency_key_digest=draft.idempotency_key_digest,
            status=ToolAuthorizationStatus.AUTHORIZED,
            invocation_id=None,
            created_at=utc_now(),
            completed_at=None,
        )

    def claim(
        self,
        *,
        invocation_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> None:
        if self.status is not ToolAuthorizationStatus.AUTHORIZED:
            raise ToolInvocationFailed("MCP write authorization was already claimed")
        key = arguments.get("idempotency_key")
        if not isinstance(key, str):
            raise InvalidToolRequest("MCP write idempotency_key is missing")
        actual = (
            binding.server_id,
            binding.server_version_id,
            binding.configuration_digest,
            binding.logical_key,
            binding.tool_name,
            binding.side_effect,
            binding.schema_digest,
            canonical_json_digest(arguments),
            canonical_json_digest(key),
        )
        expected = (
            self.server_id,
            self.server_version_id,
            self.configuration_digest,
            self.tool_key,
            self.tool_name,
            self.side_effect,
            self.schema_digest,
            self.arguments_digest,
            self.idempotency_key_digest,
        )
        if actual != expected:
            raise ToolInvocationFailed("MCP write authorization does not match this invocation")
        self.status = ToolAuthorizationStatus.EXECUTING
        self.invocation_id = invocation_id

    def settle(self, status: ToolInvocationStatus) -> None:
        if self.status is not ToolAuthorizationStatus.EXECUTING:
            raise ToolInvocationFailed("MCP write authorization is not executing")
        self.status = ToolAuthorizationStatus(status.value)
        self.completed_at = utc_now()

    def reconcile(self, status: ToolInvocationStatus) -> None:
        if self.status is not ToolAuthorizationStatus.OUTCOME_UNKNOWN:
            raise ToolInvocationFailed(
                "Only an outcome-unknown MCP write authorization can be reconciled"
            )
        if status not in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.FAILED}:
            raise ToolInvocationFailed("MCP reconciliation outcome must be terminal")
        self.status = ToolAuthorizationStatus(status.value)
        self.completed_at = utc_now()


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

    def outcome_unknown(self, error: str) -> None:
        self._require_running("mark outcome unknown")
        normalized = error.strip()
        if not normalized:
            raise ToolInvocationFailed("Unknown Tool outcome must include a safe error summary")
        self.status = ToolInvocationStatus.OUTCOME_UNKNOWN
        self.error = normalized[:2_000]
        self.completed_at = utc_now()

    def reconcile_succeeded(self, *, result_digest: str, result_bytes: int) -> None:
        self._require_unknown()
        if not result_digest.startswith("sha256:") or len(result_digest) != 71:
            raise ToolInvocationFailed("Reconciled MCP result digest must be SHA-256")
        try:
            int(result_digest.removeprefix("sha256:"), 16)
        except ValueError as exc:
            raise ToolInvocationFailed("Reconciled MCP result digest must be SHA-256") from exc
        if result_bytes < 0:
            raise ToolInvocationFailed("Reconciled MCP result size must not be negative")
        self.status = ToolInvocationStatus.SUCCEEDED
        self.result_digest = result_digest
        self.result_bytes = result_bytes
        self.error = None
        self.completed_at = utc_now()

    def reconcile_failed(self, error: str) -> None:
        self._require_unknown()
        normalized = error.strip()
        if not normalized:
            raise ToolInvocationFailed("Reconciled MCP failure must include an error summary")
        self.status = ToolInvocationStatus.FAILED
        self.result_digest = None
        self.result_bytes = None
        self.error = normalized[:2_000]
        self.completed_at = utc_now()

    def _require_unknown(self) -> None:
        if self.status is not ToolInvocationStatus.OUTCOME_UNKNOWN:
            raise ToolInvocationFailed(
                f"Cannot reconcile Tool Invocation {self.id} from {self.status.value}"
            )

    def _require_running(self, action: str) -> None:
        if self.status is not ToolInvocationStatus.RUNNING:
            raise ToolInvocationFailed(
                f"Cannot {action} Tool Invocation {self.id} from {self.status.value}"
            )
