from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .canonical import canonical_digest
from .common import (
    API_VERSION,
    KNOWN_CAPABILITIES,
    RuntimeContractError,
    UnknownCapability,
    _bounded,
    _closed,
    _digest,
    _exact_bool,
    _exact_dict,
    _exact_int,
    _exact_tuple,
    _expect_mapping,
    _reject_secrets,
    _schema,
    _text,
    _uuid,
)


@dataclass(frozen=True)
class RuntimeLimits:
    max_assignment_bytes: int = 262_144
    max_event_bytes: int = 65_536
    max_result_bytes: int = 262_144
    max_artifact_refs: int = 128

    def __post_init__(self) -> None:
        for name in (
            "max_assignment_bytes",
            "max_event_bytes",
            "max_result_bytes",
            "max_artifact_refs",
        ):
            value = getattr(self, name)
            _exact_int(value, name, minimum=1)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_assignment_bytes": self.max_assignment_bytes,
            "max_event_bytes": self.max_event_bytes,
            "max_result_bytes": self.max_result_bytes,
            "max_artifact_refs": self.max_artifact_refs,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeLimits:
        data = _expect_mapping(value, "limits")
        _closed(
            data,
            {"max_assignment_bytes", "max_event_bytes", "max_result_bytes", "max_artifact_refs"},
            "limits",
        )
        return cls(
            **{name: data.get(name, getattr(cls(), name)) for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True)
class RuntimeCapabilities:
    execution_mode: tuple[str, ...] = ("inline",)
    reattach: bool = False
    cancel: str = "cooperative"
    pause_resume: bool = False
    checkpoint: bool = False
    fork: bool = False
    event_stream: bool = False
    tool_bridge: tuple[str, ...] = ()
    artifact_io: tuple[str, ...] = ("reference",)
    isolation_profiles: tuple[str, ...] = ("trusted-in-process",)
    modalities: tuple[str, ...] = ("text",)

    def __post_init__(self) -> None:
        for name in ("reattach", "pause_resume", "checkpoint", "fork", "event_stream"):
            _exact_bool(getattr(self, name), f"capabilities.{name}")
        for name in (
            "execution_mode",
            "tool_bridge",
            "artifact_io",
            "isolation_profiles",
            "modalities",
        ):
            _exact_tuple(getattr(self, name), f"capabilities.{name}")
        if not self.execution_mode or any(
            item not in {"inline", "managed_async"} for item in self.execution_mode
        ):
            raise RuntimeContractError("capabilities.execution_mode contains an unsupported value")
        if self.cancel not in {"none", "cooperative", "forced"}:
            raise RuntimeContractError("capabilities.cancel contains an unsupported value")
        for name, values, allowed in (
            ("tool_bridge", self.tool_bridge, {"governed_action_v1"}),
            ("artifact_io", self.artifact_io, {"reference"}),
            (
                "isolation_profiles",
                self.isolation_profiles,
                {"trusted-in-process", "isolated", "remote"},
            ),
            ("modalities", self.modalities, {"text", "structured", "image", "audio"}),
        ):
            if any(item not in allowed for item in values):
                raise RuntimeContractError(f"capabilities.{name} contains an unsupported value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": list(self.execution_mode),
            "reattach": self.reattach,
            "cancel": self.cancel,
            "pause_resume": self.pause_resume,
            "checkpoint": self.checkpoint,
            "fork": self.fork,
            "event_stream": self.event_stream,
            "tool_bridge": list(self.tool_bridge),
            "artifact_io": list(self.artifact_io),
            "isolation_profiles": list(self.isolation_profiles),
            "modalities": list(self.modalities),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeCapabilities:
        data = _expect_mapping(value, "capabilities")
        _closed(data, KNOWN_CAPABILITIES, "capabilities")
        unknown = set(data) - KNOWN_CAPABILITIES
        if unknown:
            raise UnknownCapability(f"unknown runtime capabilities: {sorted(unknown)}")
        converted: dict[str, Any] = {}
        for name in KNOWN_CAPABILITIES:
            if name in data:
                item = data[name]
                if name in {
                    "execution_mode",
                    "tool_bridge",
                    "artifact_io",
                    "isolation_profiles",
                    "modalities",
                }:
                    if not isinstance(item, list):
                        raise RuntimeContractError(f"capabilities.{name} must be an array")
                    converted[name] = tuple(
                        _text(entry, f"capabilities.{name} item", max_bytes=128) for entry in item
                    )
                else:
                    converted[name] = item
        return cls(**converted)


@dataclass(frozen=True)
class RuntimeDescriptor:
    runtime_key: str
    display_name: str
    adapter_kind: str
    capabilities: RuntimeCapabilities
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_name: ClassVar[str] = "agentmesh.runtime-descriptor"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _text(self.runtime_key, "runtime_key", max_bytes=256)
        _text(self.display_name, "display_name", max_bytes=512)
        _text(self.adapter_kind, "adapter_kind", max_bytes=256)
        if type(self.capabilities) is not RuntimeCapabilities:
            raise RuntimeContractError("capabilities must be RuntimeCapabilities")
        if type(self.limits) is not RuntimeLimits:
            raise RuntimeContractError("limits must be RuntimeLimits")
        _exact_dict(self.extensions, "extensions")
        _bounded(self.extensions, path="extensions")
        _reject_secrets(self.extensions, path="extensions")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_key": self.runtime_key,
            "display_name": self.display_name,
            "adapter_kind": self.adapter_kind,
            "capabilities": self.capabilities.to_dict(),
            "limits": self.limits.to_dict(),
        }
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeDescriptor:
        data = _expect_mapping(value, "descriptor")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "version",
                "runtime_key",
                "display_name",
                "adapter_kind",
                "capabilities",
                "limits",
                "extensions",
            },
            "descriptor",
        )
        return cls(
            runtime_key=_text(data.get("runtime_key"), "runtime_key", max_bytes=256),
            display_name=_text(data.get("display_name"), "display_name", max_bytes=512),
            adapter_kind=_text(data.get("adapter_kind"), "adapter_kind", max_bytes=256),
            capabilities=RuntimeCapabilities.from_dict(data.get("capabilities", {})),
            limits=RuntimeLimits.from_dict(data.get("limits", {})),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    version_id: str
    digest: str
    size_bytes: int | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.artifact_id, "artifact_id")
        _uuid(self.version_id, "version_id")
        _digest(self.digest, "artifact digest")
        if self.size_bytes is not None:
            _exact_int(self.size_bytes, "artifact size_bytes", minimum=0)
        if self.media_type is not None:
            _text(self.media_type, "artifact media_type", max_bytes=256)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "version_id": self.version_id,
            "digest": self.digest,
        }
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        if self.media_type is not None:
            result["media_type"] = self.media_type
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactRef:
        data = _expect_mapping(value, "artifact_ref")
        _closed(
            data,
            {"artifact_id", "version_id", "digest", "size_bytes", "media_type"},
            "artifact_ref",
        )
        return cls(
            artifact_id=_uuid(data.get("artifact_id"), "artifact_id"),
            version_id=_uuid(data.get("version_id"), "version_id"),
            digest=_digest(data.get("digest"), "artifact digest") or "",
            size_bytes=data.get("size_bytes"),
            media_type=data.get("media_type"),
        )


def _artifact_refs(value: Any, name: str = "artifact_refs") -> tuple[ArtifactRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeContractError(f"{name} must be an array")
    if len(value) > 128:
        raise RuntimeContractError(f"{name} exceeds its count limit")
    return tuple(ArtifactRef.from_dict(item) for item in value)
