from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentmesh.domain.errors import FeatureDisabled, InvalidFeatureConfiguration


class Feature(str, Enum):
    AGENT_REGISTRY_MANAGEMENT = "agent_registry_management"
    AGENT_DEPLOYMENTS = "agent_deployments"
    ARTIFACT_SERVICE = "artifact_service"
    MCP_READ_TOOLS = "mcp_read_tools"
    GOVERNED_MCP = "governed_mcp"
    MODEL_TOOL_LOOP = "model_tool_loop"
    MCP_WRITE_TOOLS = "mcp_write_tools"
    A2A_FEDERATION = "a2a_federation"
    A2A_DELEGATION = "a2a_delegation"
    A2A_RECONCILIATION = "a2a_reconciliation"
    OUTCOME_RECONCILIATION = "outcome_reconciliation"
    CREDENTIAL_BROKER = "credential_broker"
    OBSERVABILITY = "observability"
    REVIEWED_EXECUTION = "reviewed_execution"
    COORDINATED_EXECUTION = "coordinated_execution"
    DYNAMIC_REPLANNING = "dynamic_replanning"
    HANDOFFS = "handoffs"
    BUDGET_ADMISSION = "budget_admission"
    QUOTA_ADMISSION = "quota_admission"
    HUMAN_RESOLUTION = "human_resolution"
    IDENTITY_RBAC = "identity_rbac"
    PERSISTENT_IDENTITY = "persistent_identity"
    POLICY_APPROVAL = "policy_approval"
    REALTIME_EVENTS = "realtime_events"


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
    Feature.GOVERNED_MCP: FeatureSpec(
        feature=Feature.GOVERNED_MCP,
        description="Versioned MCP Registry/Catalog with Policy-gated write capability admission.",
        dependencies=frozenset(
            {Feature.MCP_READ_TOOLS, Feature.IDENTITY_RBAC, Feature.POLICY_APPROVAL}
        ),
    ),
    Feature.MODEL_TOOL_LOOP: FeatureSpec(
        feature=Feature.MODEL_TOOL_LOOP,
        description=(
            "Bounded model-originated calls to Agent-version allowlisted read-only MCP Tools."
        ),
        dependencies=frozenset({Feature.GOVERNED_MCP}),
    ),
    Feature.MCP_WRITE_TOOLS: FeatureSpec(
        feature=Feature.MCP_WRITE_TOOLS,
        description="Permit-bound invocation of governed idempotent MCP write Tools.",
        dependencies=frozenset({Feature.GOVERNED_MCP}),
    ),
    Feature.A2A_FEDERATION: FeatureSpec(
        feature=Feature.A2A_FEDERATION,
        description="Trusted A2A Peer and immutable Agent Card snapshot registry.",
        dependencies=frozenset({Feature.IDENTITY_RBAC}),
    ),
    Feature.A2A_DELEGATION: FeatureSpec(
        feature=Feature.A2A_DELEGATION,
        description="Policy-governed outbound A2A Task delegation and reconciliation.",
        dependencies=frozenset(
            {Feature.A2A_FEDERATION, Feature.IDENTITY_RBAC, Feature.POLICY_APPROVAL}
        ),
    ),
    Feature.A2A_RECONCILIATION: FeatureSpec(
        feature=Feature.A2A_RECONCILIATION,
        description="Durable background polling and convergence for outbound A2A Tasks.",
        dependencies=frozenset({Feature.A2A_DELEGATION}),
    ),
    Feature.OUTCOME_RECONCILIATION: FeatureSpec(
        feature=Feature.OUTCOME_RECONCILIATION,
        description="Audited operator convergence for unknown external operation outcomes.",
        dependencies=frozenset({Feature.IDENTITY_RBAC, Feature.HUMAN_RESOLUTION}),
    ),
    Feature.CREDENTIAL_BROKER: FeatureSpec(
        feature=Feature.CREDENTIAL_BROKER,
        description="Workload-bound SecretReference and short-lived protocol Credential Broker.",
        dependencies=frozenset({Feature.PERSISTENT_IDENTITY, Feature.POLICY_APPROVAL}),
    ),
    Feature.OBSERVABILITY: FeatureSpec(
        feature=Feature.OBSERVABILITY,
        description="Task Trace correlation and Token/cost usage query APIs.",
    ),
    Feature.REVIEWED_EXECUTION: FeatureSpec(
        feature=Feature.REVIEWED_EXECUTION,
        description="Independent reviewer runs with bounded automatic revisions.",
    ),
    Feature.COORDINATED_EXECUTION: FeatureSpec(
        feature=Feature.COORDINATED_EXECUTION,
        description="Durable capability-routed Subtask DAG execution with Supervisor join.",
    ),
    Feature.DYNAMIC_REPLANNING: FeatureSpec(
        feature=Feature.DYNAMIC_REPLANNING,
        description="Immutable Goal Contracts and verified versioned Plan Patches.",
        dependencies=frozenset({Feature.COORDINATED_EXECUTION}),
    ),
    Feature.HANDOFFS: FeatureSpec(
        feature=Feature.HANDOFFS,
        description="Structured, durable Handoffs between coordinated Subtasks.",
        dependencies=frozenset({Feature.COORDINATED_EXECUTION}),
    ),
    Feature.BUDGET_ADMISSION: FeatureSpec(
        feature=Feature.BUDGET_ADMISSION,
        description="Task-level hard budgets with conservative Attempt admission reservations.",
        dependencies=frozenset({Feature.OBSERVABILITY}),
    ),
    Feature.QUOTA_ADMISSION: FeatureSpec(
        feature=Feature.QUOTA_ADMISSION,
        description="Versioned tenant/project concurrency quotas with Attempt reservations.",
        dependencies=frozenset({Feature.IDENTITY_RBAC}),
    ),
    Feature.HUMAN_RESOLUTION: FeatureSpec(
        feature=Feature.HUMAN_RESOLUTION,
        description="Immutable audit ledger and APIs for operator resolutions.",
    ),
    Feature.IDENTITY_RBAC: FeatureSpec(
        feature=Feature.IDENTITY_RBAC,
        description="Bearer Principal authentication and default-deny RBAC enforcement.",
    ),
    Feature.POLICY_APPROVAL: FeatureSpec(
        feature=Feature.POLICY_APPROVAL,
        description="Versioned Policy decisions, Approvals, and one-time execution Permits.",
        dependencies=frozenset({Feature.IDENTITY_RBAC}),
    ),
    Feature.PERSISTENT_IDENTITY: FeatureSpec(
        feature=Feature.PERSISTENT_IDENTITY,
        description="Persistent Principal/RoleBinding administration and OIDC authentication.",
        dependencies=frozenset({Feature.IDENTITY_RBAC}),
    ),
    Feature.REALTIME_EVENTS: FeatureSpec(
        feature=Feature.REALTIME_EVENTS,
        description="Tenant-filtered resumable Console updates over the domain event Stream.",
    ),
}

PROFILE_FEATURES: dict[FeatureProfile, frozenset[Feature]] = {
    FeatureProfile.MINIMAL: frozenset(),
    FeatureProfile.STANDARD: frozenset(
        {
            Feature.AGENT_REGISTRY_MANAGEMENT,
            Feature.REVIEWED_EXECUTION,
            Feature.HUMAN_RESOLUTION,
        }
    ),
    # Identity remains explicit opt-in because it requires configured credential digests.
    FeatureProfile.FULL: frozenset(
        set(Feature)
        - {
            Feature.IDENTITY_RBAC,
            Feature.PERSISTENT_IDENTITY,
            Feature.POLICY_APPROVAL,
            Feature.GOVERNED_MCP,
            Feature.MODEL_TOOL_LOOP,
            Feature.MCP_WRITE_TOOLS,
            Feature.A2A_FEDERATION,
            Feature.A2A_DELEGATION,
            Feature.A2A_RECONCILIATION,
            Feature.OUTCOME_RECONCILIATION,
            Feature.CREDENTIAL_BROKER,
            Feature.QUOTA_ADMISSION,
        }
    ),
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
