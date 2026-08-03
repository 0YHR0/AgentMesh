from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points

from fastapi import FastAPI

from agentmesh.extensions.sdk import (
    ExtensionContext,
    ExtensionHealth,
    ExtensionManifest,
    ExtensionServices,
    InvalidRuntimeExtension,
    RuntimeExtensionDefinition,
    RuntimeExtensionUnavailable,
)
from agentmesh.extensions.trust import ExtensionLock, LockedExtension
from agentmesh.features import Feature, FeatureGateSet

ENTRY_POINT_GROUP = "agentmesh.runtime_extensions"


@dataclass(frozen=True)
class RuntimeExtensionStatus:
    manifest: ExtensionManifest
    trust: LockedExtension
    enabled: bool
    health: str
    message: str
    missing_features: tuple[str, ...]
    service_keys: tuple[str, ...]


class RuntimeExtensionRegistry:
    """Discovers and validates explicitly installed trusted extensions."""

    def __init__(self, definitions: tuple[RuntimeExtensionDefinition, ...] = ()) -> None:
        self._definitions: dict[str, RuntimeExtensionDefinition] = {}
        self._trust: dict[str, LockedExtension] = {}
        self._workspace_routes: set[str] = set()
        self._asset_prefixes: set[str] = set()
        for definition in definitions:
            self.register(definition)

    @classmethod
    def discover(
        cls,
        builtins: tuple[RuntimeExtensionDefinition, ...] = (),
        *,
        lock: ExtensionLock | None = None,
    ) -> RuntimeExtensionRegistry:
        registry = cls()
        for definition in builtins:
            trust = lock.get(definition.manifest.identifier) if lock else None
            if trust is not None:
                trust.validate_manifest(definition.manifest)
            registry.register(definition, trust=trust)
        for point in entry_points(group=ENTRY_POINT_GROUP):
            trust = None
            if lock is not None:
                distribution = getattr(point, "dist", None)
                distribution_name = getattr(distribution, "name", "")
                distribution_version = getattr(distribution, "version", "")
                trust = lock.find_entry_point(distribution_name, point.name)
                if trust is None:
                    raise InvalidRuntimeExtension(
                        f"Installed extension Entry Point '{distribution_name}:{point.name}' is "
                        "not present in extensions.lock"
                    )
                if trust.version != distribution_version:
                    raise InvalidRuntimeExtension(
                        f"Extension distribution '{distribution_name}' version "
                        f"'{distribution_version}' does not match locked version '{trust.version}'"
                    )
            candidate = point.load()
            if not isinstance(candidate, RuntimeExtensionDefinition):
                raise InvalidRuntimeExtension(
                    f"Entry point '{point.name}' did not expose RuntimeExtensionDefinition"
                )
            if trust is not None:
                trust.validate_manifest(candidate.manifest)
            registry.register(candidate, trust=trust)
        return registry

    def register(
        self,
        definition: RuntimeExtensionDefinition,
        *,
        trust: LockedExtension | None = None,
    ) -> None:
        identifier = definition.manifest.identifier
        if identifier in self._definitions:
            raise InvalidRuntimeExtension(f"Extension '{identifier}' is already registered")
        for workspace in definition.manifest.workspaces:
            if workspace.route in self._workspace_routes:
                raise InvalidRuntimeExtension(
                    f"Extension workspace route '{workspace.route}' is already registered"
                )
            if workspace.asset_prefix and workspace.asset_prefix in self._asset_prefixes:
                raise InvalidRuntimeExtension(
                    f"Extension asset prefix '{workspace.asset_prefix}' is already registered"
                )
        self._definitions[identifier] = definition
        self._trust[identifier] = trust or LockedExtension.unmanaged(definition.manifest)
        self._workspace_routes.update(item.route for item in definition.manifest.workspaces)
        self._asset_prefixes.update(
            item.asset_prefix for item in definition.manifest.workspaces if item.asset_prefix
        )

    def list(self) -> tuple[RuntimeExtensionDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def get(self, identifier: str) -> RuntimeExtensionDefinition:
        try:
            return self._definitions[identifier]
        except KeyError as exc:
            raise InvalidRuntimeExtension(f"Unknown runtime extension '{identifier}'") from exc

    def trust(self, identifier: str) -> LockedExtension:
        try:
            return self._trust[identifier]
        except KeyError as exc:
            raise InvalidRuntimeExtension(f"Unknown runtime extension '{identifier}'") from exc

    def register_api(self, application: FastAPI) -> None:
        seen = {key for route in application.routes if (key := _route_key(route)) is not None}
        for definition in self.list():
            before = len(application.routes)
            definition.api_registrar(application)
            added = application.routes[before:]
            added_paths: set[str] = set()
            for route in added:
                key = _route_key(route)
                if key is None:
                    continue
                if key in seen:
                    raise InvalidRuntimeExtension(
                        f"Extension '{definition.manifest.identifier}' registered conflicting "
                        f"route '{key[0]}'"
                    )
                seen.add(key)
                added_paths.add(key[0])
            for workspace in definition.manifest.workspaces:
                if workspace.route not in added_paths:
                    raise InvalidRuntimeExtension(
                        f"Extension '{definition.manifest.identifier}' did not register declared "
                        f"workspace '{workspace.route}'"
                    )
                if workspace.asset_prefix and not any(
                    path.startswith(workspace.asset_prefix) for path in added_paths
                ):
                    raise InvalidRuntimeExtension(
                        f"Extension '{definition.manifest.identifier}' did not register declared "
                        f"asset prefix '{workspace.asset_prefix}'"
                    )


class ExtensionRuntime:
    """Loaded service instances and lifecycle state for registered extensions."""

    def __init__(
        self,
        *,
        registry: RuntimeExtensionRegistry,
        loaded: dict[str, ExtensionServices],
        enabled: frozenset[str],
        missing_features: dict[str, tuple[str, ...]],
    ) -> None:
        self._registry = registry
        self._loaded = loaded
        self._enabled = enabled
        self._missing_features = missing_features
        self._closed = False

    @classmethod
    def load(
        cls,
        registry: RuntimeExtensionRegistry,
        context: ExtensionContext,
        feature_gates: FeatureGateSet,
        enabled: str,
    ) -> ExtensionRuntime:
        available = {item.manifest.identifier for item in registry.list()}
        enabled_ids = _parse_enabled(enabled, available)
        loaded: dict[str, ExtensionServices] = {}
        missing_features: dict[str, tuple[str, ...]] = {}
        for identifier in sorted(enabled_ids):
            definition = registry.get(identifier)
            missing_services = tuple(
                key for key in definition.manifest.required_core_services if not context.has(key)
            )
            if missing_services:
                names = ", ".join(missing_services)
                raise InvalidRuntimeExtension(
                    f"Extension '{identifier}' requires unavailable core service(s): {names}"
                )
            values = dict(definition.service_factory(context))
            declared = set(definition.manifest.provided_services)
            if set(values) != declared:
                raise InvalidRuntimeExtension(
                    f"Extension '{identifier}' service factory must provide exactly: "
                    + ", ".join(sorted(declared))
                )
            loaded[identifier] = ExtensionServices(identifier, values)
            missing_features[identifier] = tuple(
                feature
                for feature in definition.manifest.required_features
                if not feature_gates.is_enabled(Feature(feature))
            )
        return cls(
            registry=registry,
            loaded=loaded,
            enabled=frozenset(enabled_ids),
            missing_features=missing_features,
        )

    def require_loaded(self, identifier: str) -> ExtensionServices:
        value = self._loaded.get(identifier)
        if value is None:
            raise RuntimeExtensionUnavailable(f"Runtime extension '{identifier}' is disabled")
        return value

    def require_service(self, identifier: str, service_key: str) -> object:
        return self.require_loaded(identifier).require(service_key)

    def statuses(self) -> tuple[RuntimeExtensionStatus, ...]:
        values: list[RuntimeExtensionStatus] = []
        for definition in self._registry.list():
            identifier = definition.manifest.identifier
            services = self._loaded.get(identifier)
            if services is None:
                values.append(
                    RuntimeExtensionStatus(
                        manifest=definition.manifest,
                        trust=self._registry.trust(identifier),
                        enabled=False,
                        health="disabled",
                        message="Extension is installed but disabled",
                        missing_features=(),
                        service_keys=(),
                    )
                )
                continue
            missing = self._missing_features.get(identifier, ())
            try:
                probe = definition.health_probe(services)
            except Exception as exc:  # pragma: no cover - defensive boundary
                probe = ExtensionHealth(status="unhealthy", message=str(exc))
            health = probe.status
            message = probe.message
            if missing and health == "ready":
                health = "degraded"
                message = "Required Features are disabled: " + ", ".join(missing)
            values.append(
                RuntimeExtensionStatus(
                    manifest=definition.manifest,
                    trust=self._registry.trust(identifier),
                    enabled=True,
                    health=health,
                    message=message,
                    missing_features=missing,
                    service_keys=services.keys(),
                )
            )
        return tuple(values)

    def close(self) -> None:
        if self._closed:
            return
        for definition in reversed(self._registry.list()):
            services = self._loaded.get(definition.manifest.identifier)
            if services is not None:
                definition.stop_callback(services)
        self._closed = True


def _parse_enabled(value: str, available: set[str]) -> set[str]:
    requested = {item.strip() for item in value.split(",") if item.strip()}
    if requested == {"*"}:
        return set(available)
    if "*" in requested:
        raise InvalidRuntimeExtension("'*' cannot be combined with explicit extension identifiers")
    unknown = requested - available
    if unknown:
        raise InvalidRuntimeExtension(
            "Unknown enabled runtime extension(s): " + ", ".join(sorted(unknown))
        )
    return requested


def _route_key(route: object) -> tuple[str, frozenset[str]] | None:
    path = getattr(route, "path", None)
    if not isinstance(path, str):
        return None
    methods = frozenset(getattr(route, "methods", None) or ())
    return path, methods
