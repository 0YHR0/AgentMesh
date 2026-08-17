from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from .canonical import canonical_digest, canonical_json_bytes, normalize_utc
from .common import (
    API_VERSION,
    KNOWN_CAPABILITIES,
    KNOWN_OBLIGATIONS,
    RuntimeContractError,
    UnknownCapability,
    UnknownSecurityObligation,
    _bounded,
    _closed,
    _digest,
    _exact_dict,
    _exact_int,
    _exact_tuple,
    _expect_mapping,
    _id_list,
    _reject_secrets,
    _schema,
    _text,
    _timestamp,
    _uuid,
    _validate_required_capabilities,
)
from .descriptor import ArtifactRef, _artifact_refs


@dataclass(frozen=True)
class RuntimeAssignment:
    assignment_id: str
    tenant_id: str
    task_id: str
    run_id: str
    agent_definition_id: str
    agent_version_id: str
    agent_version_digest: str
    runtime_version_id: str
    runtime_descriptor_digest: str
    execution_mode: str
    run_role: str
    revision: int
    objective: str | None = None
    structured_input: Mapping[str, Any] | None = None
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    work_item_snapshot_version: int | None = None
    work_item_snapshot_digest: str | None = None
    acceptance_contract: Mapping[str, Any] = field(default_factory=dict)
    output_schema_digest: str | None = None
    required_capabilities: Mapping[str, Any] = field(default_factory=dict)
    tool_profile_version: str | None = None
    tool_snapshot_refs: tuple[str, ...] = ()
    capability_bundle_refs: tuple[str, ...] = ()
    policy_snapshot_ref: str | None = None
    required_obligations: tuple[str, ...] = ()
    principal_context_ref: str | None = None
    delegation_grant_ref: str | None = None
    budget_slice: Mapping[str, Any] = field(default_factory=dict)
    deadline: datetime | None = None
    per_operation_limits: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    allowed_artifact_operations: tuple[str, ...] = ()
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    assignment_digest: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_name: ClassVar[str] = "agentmesh.runtime-assignment"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in (
            "assignment_id",
            "task_id",
            "run_id",
            "agent_definition_id",
            "agent_version_id",
            "runtime_version_id",
        ):
            _uuid(getattr(self, name), name)
        _text(self.tenant_id, "tenant_id", max_bytes=256)
        for name in (
            "agent_version_digest",
            "runtime_descriptor_digest",
            "output_schema_digest",
            "work_item_snapshot_digest",
        ):
            if getattr(self, name) is not None:
                _digest(getattr(self, name), name, required=False)
        _text(self.execution_mode, "execution_mode", max_bytes=64)
        if self.execution_mode not in {"inline", "managed_async"}:
            raise RuntimeContractError("execution_mode contains an unsupported value")
        _text(self.run_role, "run_role", max_bytes=64)
        _exact_int(self.revision, "revision", minimum=0)
        if self.work_item_snapshot_version is not None:
            _exact_int(self.work_item_snapshot_version, "work_item_snapshot_version", minimum=0)
        if self.objective is not None:
            _text(self.objective, "objective")
        if self.structured_input is not None:
            _exact_dict(self.structured_input, "structured_input")
        for name in (
            "acceptance_contract",
            "required_capabilities",
            "budget_slice",
            "per_operation_limits",
            "trace_context",
            "correlation_ids",
            "extensions",
        ):
            _exact_dict(getattr(self, name), name)
        for name in (
            "input_artifact_refs",
            "tool_snapshot_refs",
            "capability_bundle_refs",
            "required_obligations",
            "artifact_refs",
            "allowed_artifact_operations",
        ):
            _exact_tuple(getattr(self, name), name)
        for name in (
            "tool_snapshot_refs",
            "capability_bundle_refs",
            "required_obligations",
            "allowed_artifact_operations",
        ):
            if any(type(item) is not str for item in getattr(self, name)):
                raise RuntimeContractError("assignment reference values must be strings")
        if any(
            type(key) is not str or type(value) is not str
            for key, value in self.correlation_ids.items()
        ):
            raise RuntimeContractError("correlation identifiers must be strings")
        for _name, refs in (
            ("input_artifact_refs", self.input_artifact_refs),
            ("artifact_refs", self.artifact_refs),
        ):
            if len(refs) > 128:
                raise RuntimeContractError("artifact reference count limit exceeded")
            if any(type(ref) is not ArtifactRef for ref in refs):
                raise RuntimeContractError("artifact references required")
        if (
            self.objective is None
            and not self.input_artifact_refs
            and self.structured_input is None
        ):
            raise RuntimeContractError(
                "assignment requires objective, structured input, or input artifacts"
            )
        if self.structured_input is not None:
            _bounded(self.structured_input, path="structured_input")
            _reject_secrets(self.structured_input, path="structured_input")
        for name, value in (
            ("acceptance_contract", self.acceptance_contract),
            ("required_capabilities", self.required_capabilities),
            ("budget_slice", self.budget_slice),
            ("per_operation_limits", self.per_operation_limits),
            ("trace_context", self.trace_context),
            ("correlation_ids", self.correlation_ids),
            ("extensions", self.extensions),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        unknown_obligations = set(self.required_obligations) - KNOWN_OBLIGATIONS
        if unknown_obligations:
            raise UnknownSecurityObligation("unsupported security obligation")
        unknown_capabilities = set(self.required_capabilities) - KNOWN_CAPABILITIES
        if unknown_capabilities:
            raise UnknownCapability("unsupported capability")
        _validate_required_capabilities(self.required_capabilities)
        if self.deadline is not None:
            normalize_utc(self.deadline)
        if self.assignment_digest is None:
            object.__setattr__(self, "assignment_digest", self.digest())
        else:
            _digest(self.assignment_digest, "assignment_digest")
            if self.assignment_digest != self.digest():
                raise RuntimeContractError("assignment_digest does not match canonical assignment")
        if len(canonical_json_bytes(self.to_dict())) > 262_144:
            raise RuntimeContractError("assignment exceeds the 256 KiB limit")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "assignment_id": self.assignment_id,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "agent_definition_id": self.agent_definition_id,
            "agent_version_id": self.agent_version_id,
            "agent_version_digest": self.agent_version_digest,
            "runtime_version_id": self.runtime_version_id,
            "runtime_descriptor_digest": self.runtime_descriptor_digest,
            "execution_mode": self.execution_mode,
            "run_role": self.run_role,
            "revision": self.revision,
            "acceptance_contract": dict(self.acceptance_contract),
            "required_capabilities": dict(self.required_capabilities),
            "tool_snapshot_refs": list(self.tool_snapshot_refs),
            "capability_bundle_refs": list(self.capability_bundle_refs),
            "required_obligations": list(self.required_obligations),
            "budget_slice": dict(self.budget_slice),
            "per_operation_limits": dict(self.per_operation_limits),
            "allowed_artifact_operations": list(self.allowed_artifact_operations),
            "trace_context": dict(self.trace_context),
            "correlation_ids": dict(self.correlation_ids),
            "input_artifact_refs": [item.to_dict() for item in self.input_artifact_refs],
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }
        optional = {
            "objective": self.objective,
            "structured_input": dict(self.structured_input)
            if self.structured_input is not None
            else None,
            "work_item_snapshot_version": self.work_item_snapshot_version,
            "work_item_snapshot_digest": self.work_item_snapshot_digest,
            "output_schema_digest": self.output_schema_digest,
            "tool_profile_version": self.tool_profile_version,
            "policy_snapshot_ref": self.policy_snapshot_ref,
            "principal_context_ref": self.principal_context_ref,
            "delegation_grant_ref": self.delegation_grant_ref,
            "deadline": self.deadline,
        }
        result.update({name: value for name, value in optional.items() if value is not None})
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        if include_digest:
            result["assignment_digest"] = self.assignment_digest
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeAssignment:
        data = _expect_mapping(value, "assignment")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "assignment_id",
                "tenant_id",
                "task_id",
                "run_id",
                "agent_definition_id",
                "agent_version_id",
                "agent_version_digest",
                "runtime_version_id",
                "runtime_descriptor_digest",
                "execution_mode",
                "run_role",
                "revision",
                "objective",
                "structured_input",
                "input_artifact_refs",
                "work_item_snapshot_version",
                "work_item_snapshot_digest",
                "acceptance_contract",
                "output_schema_digest",
                "required_capabilities",
                "tool_profile_version",
                "tool_snapshot_refs",
                "capability_bundle_refs",
                "policy_snapshot_ref",
                "required_obligations",
                "principal_context_ref",
                "delegation_grant_ref",
                "budget_slice",
                "deadline",
                "per_operation_limits",
                "artifact_refs",
                "allowed_artifact_operations",
                "trace_context",
                "correlation_ids",
                "assignment_digest",
                "extensions",
            },
            "assignment",
        )
        return cls(
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            tenant_id=_text(data.get("tenant_id"), "tenant_id", max_bytes=256),
            task_id=_uuid(data.get("task_id"), "task_id"),
            run_id=_uuid(data.get("run_id"), "run_id"),
            agent_definition_id=_uuid(data.get("agent_definition_id"), "agent_definition_id"),
            agent_version_id=_uuid(data.get("agent_version_id"), "agent_version_id"),
            agent_version_digest=_digest(data.get("agent_version_digest"), "agent_version_digest")
            or "",
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            runtime_descriptor_digest=_digest(
                data.get("runtime_descriptor_digest"), "runtime_descriptor_digest"
            )
            or "",
            execution_mode=_text(data.get("execution_mode"), "execution_mode", max_bytes=64),
            run_role=_text(data.get("run_role"), "run_role", max_bytes=64),
            revision=data.get("revision"),
            objective=data.get("objective"),
            structured_input=data.get("structured_input"),
            input_artifact_refs=_artifact_refs(data.get("input_artifact_refs")),
            work_item_snapshot_version=data.get("work_item_snapshot_version"),
            work_item_snapshot_digest=_digest(
                data.get("work_item_snapshot_digest"), "work_item_snapshot_digest", required=False
            ),
            acceptance_contract=_expect_mapping(
                data.get("acceptance_contract", {}), "acceptance_contract"
            ),
            output_schema_digest=_digest(
                data.get("output_schema_digest"), "output_schema_digest", required=False
            ),
            required_capabilities=_expect_mapping(
                data.get("required_capabilities", {}), "required_capabilities"
            ),
            tool_profile_version=data.get("tool_profile_version"),
            tool_snapshot_refs=_id_list(data.get("tool_snapshot_refs"), "tool_snapshot_refs"),
            capability_bundle_refs=_id_list(
                data.get("capability_bundle_refs"), "capability_bundle_refs"
            ),
            policy_snapshot_ref=data.get("policy_snapshot_ref"),
            required_obligations=tuple(
                _text(item, "required_obligations item", max_bytes=128)
                for item in data.get("required_obligations", [])
            ),
            principal_context_ref=data.get("principal_context_ref"),
            delegation_grant_ref=data.get("delegation_grant_ref"),
            budget_slice=_expect_mapping(data.get("budget_slice", {}), "budget_slice"),
            deadline=_timestamp(data.get("deadline"), "deadline", required=False),
            per_operation_limits=_expect_mapping(
                data.get("per_operation_limits", {}), "per_operation_limits"
            ),
            artifact_refs=_artifact_refs(data.get("artifact_refs")),
            allowed_artifact_operations=tuple(
                _text(item, "allowed_artifact_operations item", max_bytes=64)
                for item in data.get("allowed_artifact_operations", [])
            ),
            trace_context=_expect_mapping(data.get("trace_context", {}), "trace_context"),
            correlation_ids={
                str(k): _text(v, f"correlation_ids.{k}", max_bytes=512)
                for k, v in _expect_mapping(
                    data.get("correlation_ids", {}), "correlation_ids"
                ).items()
            },
            assignment_digest=_digest(
                data.get("assignment_digest"), "assignment_digest", required=False
            ),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )


@dataclass(frozen=True)
class RuntimeExecutionHandle:
    runtime_execution_id: str
    runtime_version_id: str
    provider_execution_ref: str
    assignment_id: str
    assignment_digest: str
    created_at: datetime
    provider_generation: str | None = None
    schema_name: ClassVar[str] = "agentmesh.runtime-execution-handle"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in ("runtime_execution_id", "runtime_version_id", "assignment_id"):
            _uuid(getattr(self, name), name)
        _text(self.provider_execution_ref, "provider_execution_ref", max_bytes=4096)
        _reject_secrets({"provider_execution_ref": self.provider_execution_ref}, path="handle")
        _digest(self.assignment_digest, "assignment_digest")
        if self.provider_generation is not None:
            _text(self.provider_generation, "provider_generation", max_bytes=256)
        _timestamp(self.created_at, "created_at")
        if len(canonical_json_bytes(self.to_dict())) > 65_536:
            raise RuntimeContractError("execution handle exceeds the event size limit")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_execution_id": self.runtime_execution_id,
            "runtime_version_id": self.runtime_version_id,
            "provider_execution_ref": self.provider_execution_ref,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "created_at": self.created_at,
        }
        if self.provider_generation is not None:
            result["provider_generation"] = self.provider_generation
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeExecutionHandle:
        data = _expect_mapping(value, "execution_handle")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "runtime_execution_id",
                "runtime_version_id",
                "provider_execution_ref",
                "provider_generation",
                "assignment_id",
                "assignment_digest",
                "created_at",
            },
            "execution_handle",
        )
        return cls(
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            provider_execution_ref=_text(
                data.get("provider_execution_ref"), "provider_execution_ref", max_bytes=4096
            ),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            created_at=_timestamp(data.get("created_at"), "created_at"),
            provider_generation=data.get("provider_generation"),
        )
