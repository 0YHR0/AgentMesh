from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from agentmesh.features import Feature
from agentmesh.packs.sdk import CompanyTemplateDefinition

if TYPE_CHECKING:
    from fastapi import FastAPI

RUNTIME_EXTENSION_API_VERSION = "0.1"
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class InvalidRuntimeExtension(ValueError):
    """Raised when a trusted extension violates the runtime contract."""


class RuntimeExtensionUnavailable(RuntimeError):
    """Raised when a disabled or failed extension service is requested."""


class CoreServiceKey(str, Enum):
    """Stable names for core capabilities exposed to trusted extensions."""

    UNIT_OF_WORK_FACTORY = "unit_of_work_factory"
    TASKS = "tasks"
    AGENT_REGISTRY = "agent_registry"
    ARTIFACTS = "artifacts"
    BUSINESS_OBJECTS = "business_objects"
    COMPANY_PACKS = "company_packs"
    CREDENTIALS = "credentials"
    MEMORY = "memory"
    POLICIES = "policies"


@dataclass(frozen=True)
class ExtensionWorkspace:
    route: str
    name: str
    asset_prefix: str | None = None

    def __post_init__(self) -> None:
        if not self.route.startswith("/") or self.route == "/":
            raise InvalidRuntimeExtension(
                "Extension workspace route must be an absolute non-root path"
            )
        if not self.name.strip():
            raise InvalidRuntimeExtension("Extension workspace name is required")
        if self.asset_prefix is not None and not self.asset_prefix.startswith("/"):
            raise InvalidRuntimeExtension("Extension asset prefix must be an absolute path")


@dataclass(frozen=True)
class ExtensionManifest:
    identifier: str
    name: str
    version: str
    description: str
    required_core_services: tuple[str, ...]
    provided_services: tuple[str, ...]
    required_features: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    workspaces: tuple[ExtensionWorkspace, ...] = ()
    external_writes_enabled: bool = False
    api_version: str = RUNTIME_EXTENSION_API_VERSION

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.identifier):
            raise InvalidRuntimeExtension(f"Invalid extension identifier '{self.identifier}'")
        if not self.name.strip() or not self.description.strip():
            raise InvalidRuntimeExtension("Extension name and description are required")
        if not _VERSION.fullmatch(self.version):
            raise InvalidRuntimeExtension(f"Invalid extension version '{self.version}'")
        if self.api_version != RUNTIME_EXTENSION_API_VERSION:
            raise InvalidRuntimeExtension(
                f"Unsupported runtime extension API version '{self.api_version}'"
            )
        self._require_unique("required core service", self.required_core_services)
        self._require_unique("provided service", self.provided_services)
        self._require_unique("required Feature", self.required_features)
        self._require_unique("required Credential", self.required_credentials)
        self._require_unique("permission", self.permissions)
        if not self.provided_services:
            raise InvalidRuntimeExtension("Extension must declare at least one provided service")
        for feature in self.required_features:
            try:
                Feature(feature)
            except ValueError as exc:
                raise InvalidRuntimeExtension(
                    f"Extension requires unknown Feature '{feature}'"
                ) from exc

    @staticmethod
    def _require_unique(label: str, values: tuple[str, ...]) -> None:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise InvalidRuntimeExtension(f"Extension {label} names cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise InvalidRuntimeExtension(f"Extension {label} names must be unique")


@dataclass(frozen=True)
class ExtensionHealth:
    status: Literal["ready", "degraded", "unhealthy"]
    message: str = ""


class ExtensionContext:
    """Capability-limited context supplied by the AgentMesh composition root."""

    def __init__(self, *, tenant_id: str, services: Mapping[str, object]) -> None:
        self.tenant_id = tenant_id
        self._services = MappingProxyType(dict(services))

    def require(self, key: str | CoreServiceKey) -> object:
        value = self._services.get(str(key.value if isinstance(key, CoreServiceKey) else key))
        if value is None:
            raise InvalidRuntimeExtension(f"Required core service '{key}' is unavailable")
        return value

    def has(self, key: str | CoreServiceKey) -> bool:
        normalized = key.value if isinstance(key, CoreServiceKey) else key
        return normalized in self._services


class ExtensionServices:
    """Immutable, extension-scoped service collection."""

    def __init__(self, identifier: str, services: Mapping[str, object]) -> None:
        self.identifier = identifier
        self._services = MappingProxyType(dict(services))

    def require(self, key: str) -> object:
        value = self._services.get(key)
        if value is None:
            raise RuntimeExtensionUnavailable(
                f"Extension '{self.identifier}' does not provide service '{key}'"
            )
        return value

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._services))


ServiceFactory = Callable[[ExtensionContext], Mapping[str, object]]
ApiRegistrar = Callable[["FastAPI"], None]
HealthProbe = Callable[[ExtensionServices], ExtensionHealth]
StopCallback = Callable[[ExtensionServices], None]


def _ready(_: ExtensionServices) -> ExtensionHealth:
    return ExtensionHealth(status="ready")


def _stop(_: ExtensionServices) -> None:
    return None


@dataclass(frozen=True)
class RuntimeExtensionDefinition:
    """Executable contract for an explicitly trusted in-process extension."""

    manifest: ExtensionManifest
    service_factory: ServiceFactory
    api_registrar: ApiRegistrar
    company_templates: tuple[CompanyTemplateDefinition, ...] = ()
    health_probe: HealthProbe = _ready
    stop_callback: StopCallback = _stop
