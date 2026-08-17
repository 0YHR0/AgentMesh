from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from .canonical import canonical_digest, canonical_json_bytes
from .common import (
    API_VERSION,
    RuntimeContractError,
    RuntimePhase,
    _bounded,
    _closed,
    _digest,
    _exact_bool,
    _exact_dict,
    _exact_tuple,
    _expect_mapping,
    _id_list,
    _reject_secrets,
    _schema,
    _text,
    _uuid,
)
from .descriptor import ArtifactRef, _artifact_refs


@dataclass(frozen=True)
class RuntimeResult:
    runtime_execution_id: str
    assignment_id: str
    assignment_digest: str
    runtime_version_id: str
    agent_version_id: str
    output: Any | None
    output_artifact_refs: tuple[ArtifactRef, ...]
    usage: Mapping[str, Any]
    usage_estimated: bool
    terminal_phase: RuntimePhase
    safe_summary: str
    produced_artifact_refs: tuple[ArtifactRef, ...] = ()
    governed_action_evidence_refs: tuple[str, ...] = ()
    result_digest: str | None = None
    schema_name: ClassVar[str] = "agentmesh.runtime-result"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in (
            "runtime_execution_id",
            "assignment_id",
            "runtime_version_id",
            "agent_version_id",
        ):
            _uuid(getattr(self, name), name)
        _digest(self.assignment_digest, "assignment_digest")
        if type(self.terminal_phase) is not RuntimePhase:
            raise RuntimeContractError("terminal_phase must be RuntimePhase")
        if self.terminal_phase is not RuntimePhase.SUCCEEDED:
            raise RuntimeContractError("RuntimeResult represents terminal success only")
        _exact_bool(self.usage_estimated, "usage_estimated")
        _exact_dict(self.usage, "usage")
        _exact_tuple(self.output_artifact_refs, "output_artifact_refs")
        _exact_tuple(self.produced_artifact_refs, "produced_artifact_refs")
        _exact_tuple(self.governed_action_evidence_refs, "governed_action_evidence_refs")
        if any(
            type(ref) is not ArtifactRef
            for ref in self.output_artifact_refs + self.produced_artifact_refs
        ):
            raise RuntimeContractError("result ArtifactRefs must contain ArtifactRef values")
        for ref in self.governed_action_evidence_refs:
            _text(ref, "governed_action_evidence_refs item", max_bytes=1024)
        if self.output is None and not self.output_artifact_refs:
            raise RuntimeContractError("successful result requires output or ArtifactRefs")
        _bounded(self.output, path="output")
        _reject_secrets(self.output, path="output")
        _bounded(self.usage, path="usage")
        _text(self.safe_summary, "safe_summary", max_bytes=4096)
        if self.result_digest is not None:
            _digest(self.result_digest, "result_digest", required=False)
            if self.result_digest != self.digest():
                raise RuntimeContractError("result_digest does not match canonical result")
        else:
            object.__setattr__(self, "result_digest", self.digest())
        if len(canonical_json_bytes(self.to_dict())) > 262_144:
            raise RuntimeContractError("result exceeds the 256 KiB limit")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "runtime_version_id": self.runtime_version_id,
            "agent_version_id": self.agent_version_id,
            "output_artifact_refs": [ref.to_dict() for ref in self.output_artifact_refs],
            "usage": dict(self.usage),
            "usage_estimated": self.usage_estimated,
            "terminal_phase": self.terminal_phase,
            "safe_summary": self.safe_summary,
            "produced_artifact_refs": [ref.to_dict() for ref in self.produced_artifact_refs],
            "governed_action_evidence_refs": list(self.governed_action_evidence_refs),
        }
        if self.output is not None:
            result["output"] = self.output
        if include_digest:
            result["result_digest"] = self.result_digest
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeResult:
        data = _expect_mapping(value, "result")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "version",
                "runtime_execution_id",
                "assignment_id",
                "assignment_digest",
                "runtime_version_id",
                "agent_version_id",
                "output",
                "output_artifact_refs",
                "usage",
                "usage_estimated",
                "terminal_phase",
                "safe_summary",
                "produced_artifact_refs",
                "governed_action_evidence_refs",
                "result_digest",
            },
            "result",
        )
        try:
            phase = RuntimePhase(data.get("terminal_phase"))
        except ValueError as exc:
            raise RuntimeContractError("terminal_phase must be a known RuntimePhase") from exc
        return cls(
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            agent_version_id=_uuid(data.get("agent_version_id"), "agent_version_id"),
            output=data.get("output"),
            output_artifact_refs=_artifact_refs(
                data.get("output_artifact_refs"), "output_artifact_refs"
            ),
            usage=_expect_mapping(data.get("usage", {}), "usage"),
            usage_estimated=data.get("usage_estimated", False),
            terminal_phase=phase,
            safe_summary=_text(data.get("safe_summary"), "safe_summary", max_bytes=4096),
            produced_artifact_refs=_artifact_refs(
                data.get("produced_artifact_refs"), "produced_artifact_refs"
            ),
            governed_action_evidence_refs=_id_list(
                data.get("governed_action_evidence_refs"), "governed_action_evidence_refs"
            ),
            result_digest=_digest(data.get("result_digest"), "result_digest", required=False),
        )
