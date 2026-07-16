from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentmesh.domain.errors import FeatureDisabled, InvalidFeatureConfiguration


class Feature(str, Enum):
    AGENT_REGISTRY_MANAGEMENT = "agent_registry_management"
    AGENT_DEPLOYMENTS = "agent_deployments"
    ARTIFACT_SERVICE = "artifact_service"
    MCP_READ_TOOLS = "mcp_read_tools"


class FeatureProfile(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"


@dataclass(frozen=True)
class FeatureSpec:
    feature: Feature
    description: str
    dependencies: frozenset[Feature] = frozenset()


@dataclass(frozen=True)
class FeatureState:
    feature: Feature
    enabled: bool
    description: str
    dependencies: tuple[Feature, ...]


FEATURE_SPECS: dict[Feature, FeatureSpec] = {
    Feature.AGENT_REGISTRY_MANAGEMENT: FeatureSpec(
        feature=Feature.AGENT_REGISTRY_MANAGEMENT,
        description="Public APIs for managing agent definitions, versions, and capabilities.",
    ),
    Feature.AGENT_DEPLOYMENTS: FeatureSpec(
        feature=Feature.AGENT_DEPLOYMENTS,
        description="APIs for managing agent deployments and runtime instances.",
        dependencies=frozenset({Feature.AGENT_REGISTRY_MANAGEMENT}),
    ),
    Feature.ARTIFACT_SERVICE: FeatureSpec(
        feature=Feature.ARTIFACT_SERVICE,
        description="APIs for creating, versioning, and downloading managed Artifacts.",
    ),
    Feature.MCP_READ_TOOLS: FeatureSpec(
        feature=Feature.MCP_READ_TOOLS,
        description="Explicit invocation of allowlisted read-only MCP Tools.",
    ),
}

PROFILE_FEATURES: dict[FeatureProfile, frozenset[Feature]] = {
    FeatureProfile.MINIMAL: frozenset(),
    FeatureProfile.STANDARD: frozenset({Feature.AGENT_REGISTRY_MANAGEMENT}),
    FeatureProfile.FULL: frozenset(Feature),
}


@dataclass(frozen=True)
class FeatureGateSet:
    """Immutable startup configuration for optional AgentMesh capabilities."""

    profile: FeatureProfile
    enabled_features: frozenset[Feature]

    @classmethod
    def from_config(cls, profile: str, overrides: str = "") -> FeatureGateSet:
        try:
            selected_profile = FeatureProfile(profile.strip().lower())
        except ValueError as exc:
            supported = ", ".join(value.value for value in FeatureProfile)
            raise InvalidFeatureConfiguration(
                f"Unknown feature profile '{profile}'. Supported profiles: {supported}"
            ) from exc

        enabled = set(PROFILE_FEATURES[selected_profile])
        seen: set[Feature] = set()
        for assignment in filter(None, (item.strip() for item in overrides.split(","))):
            if assignment.count("=") != 1:
                raise InvalidFeatureConfiguration(
                    f"Invalid feature override '{assignment}'; expected feature=true|false"
                )
            raw_feature, raw_enabled = (part.strip() for part in assignment.split("=", 1))
            try:
                feature = Feature(raw_feature)
            except ValueError as exc:
                supported = ", ".join(value.value for value in Feature)
                raise InvalidFeatureConfiguration(
                    f"Unknown feature '{raw_feature}'. Supported features: {supported}"
                ) from exc
            if feature in seen:
                raise InvalidFeatureConfiguration(
                    f"Feature '{feature.value}' is configured more than once"
                )
            seen.add(feature)

            if raw_enabled == "true":
                enabled.add(feature)
            elif raw_enabled == "false":
                enabled.discard(feature)
            else:
                raise InvalidFeatureConfiguration(
                    f"Invalid value '{raw_enabled}' for feature '{feature.value}'; "
                    "expected true or false"
                )

        cls._validate_dependencies(enabled)
        return cls(profile=selected_profile, enabled_features=frozenset(enabled))

    @staticmethod
    def _validate_dependencies(enabled: set[Feature]) -> None:
        for feature in enabled:
            missing = FEATURE_SPECS[feature].dependencies - enabled
            if missing:
                dependencies = ", ".join(sorted(value.value for value in missing))
                raise InvalidFeatureConfiguration(
                    f"Feature '{feature.value}' requires enabled feature(s): {dependencies}"
                )

    def is_enabled(self, feature: Feature) -> bool:
        return feature in self.enabled_features

    def require(self, feature: Feature) -> None:
        if not self.is_enabled(feature):
            raise FeatureDisabled(feature.value, self.profile.value)

    def states(self) -> tuple[FeatureState, ...]:
        return tuple(
            FeatureState(
                feature=feature,
                enabled=self.is_enabled(feature),
                description=spec.description,
                dependencies=tuple(sorted(spec.dependencies, key=lambda value: value.value)),
            )
            for feature, spec in FEATURE_SPECS.items()
        )
