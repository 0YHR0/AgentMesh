from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentmesh.domain.errors import FeatureDisabled, InvalidFeatureConfiguration


class Feature(str, Enum):
    MANAGED_AGENT_RUNTIME = "managed_agent_runtime"
    GENERIC_SUBPROCESS_RUNTIME = "generic_subprocess_runtime"
    MANAGED_RUNTIME_WORKER = "managed_runtime_worker"
    MANAGED_RUNTIME_DIRECT_CUTOVER = "managed_runtime_direct_cutover"
    DUAL_RECORD_RUNTIME = "dual_record_runtime"
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
    ACTIVITY_TIMELINE = "activity_timeline"
    OFFICE_3D = "office_3d"
    COMPANY_MODEL = "company_model"
    COMPANY_GOALS = "company_goals"
    COMPANY_OPERATIONS = "company_operations"
    BUSINESS_OBJECTS = "business_objects"
    ORGANIZATIONAL_MEMORY = "organizational_memory"
    COMPANY_FINANCE_READ = "company_finance_read"
    FINANCIAL_GOVERNANCE = "financial_governance"
    COMPANY_PACKS = "company_packs"


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
    Feature.MANAGED_AGENT_RUNTIME: FeatureSpec(
        feature=Feature.MANAGED_AGENT_RUNTIME,
        description="Framework-neutral Runtime Registry, execution evidence, and operator reads.",
    ),
    Feature.GENERIC_SUBPROCESS_RUNTIME: FeatureSpec(
        feature=Feature.GENERIC_SUBPROCESS_RUNTIME,
        description=(
            "Explicit opt-in for the A3 reference subprocess runtime proof; "
            "it is not enabled by any default profile."
        ),
        dependencies=frozenset({Feature.MANAGED_AGENT_RUNTIME}),
    ),
    Feature.MANAGED_RUNTIME_WORKER: FeatureSpec(
        feature=Feature.MANAGED_RUNTIME_WORKER,
        description=(
            "Explicit opt-in for newly admitted, Runtime-pinned worker executions; "
            "this does not change the legacy authoritative path."
        ),
        dependencies=frozenset({Feature.MANAGED_AGENT_RUNTIME}),
    ),
    Feature.MANAGED_RUNTIME_DIRECT_CUTOVER: FeatureSpec(
        feature=Feature.MANAGED_RUNTIME_DIRECT_CUTOVER,
        description=(
            "CI-only admission of new deterministic DIRECT Runs to built-in LangGraph v2; "
            "existing Runs keep their immutable authority."
        ),
        dependencies=frozenset({Feature.MANAGED_RUNTIME_WORKER}),
    ),
    Feature.DUAL_RECORD_RUNTIME: FeatureSpec(
        feature=Feature.DUAL_RECORD_RUNTIME,
        description="Deterministic comparison recording for the managed Runtime path.",
        dependencies=frozenset({Feature.MANAGED_RUNTIME_WORKER}),
    ),
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
    Feature.ACTIVITY_TIMELINE: FeatureSpec(
        feature=Feature.ACTIVITY_TIMELINE,
        description="Tenant-safe cross-domain Task activity projection and Console timeline.",
    ),
    Feature.OFFICE_3D: FeatureSpec(
        feature=Feature.OFFICE_3D,
        description="Experimental GPU-rendered AgentMesh Office 2.5D operator surface.",
    ),
    Feature.COMPANY_MODEL: FeatureSpec(
        feature=Feature.COMPANY_MODEL,
        description=(
            "Durable Company, Organization Unit, Position, Appointment, and relationship graph."
        ),
        dependencies=frozenset({Feature.AGENT_REGISTRY_MANAGEMENT}),
    ),
    Feature.COMPANY_GOALS: FeatureSpec(
        feature=Feature.COMPANY_GOALS,
        description="Operating Cycles, Objectives, Key Results, Initiatives, and Task lineage.",
        dependencies=frozenset({Feature.COMPANY_MODEL}),
    ),
    Feature.COMPANY_OPERATIONS: FeatureSpec(
        feature=Feature.COMPANY_OPERATIONS,
        description=(
            "Governed recurring Operations with deterministic triggers, occurrences, "
            "Task lineage, and exception evidence."
        ),
        dependencies=frozenset({Feature.COMPANY_GOALS}),
    ),
    Feature.BUSINESS_OBJECTS: FeatureSpec(
        feature=Feature.BUSINESS_OBJECTS,
        description=(
            "Versioned typed business records with named lifecycle actions and "
            "append-only revision evidence."
        ),
        dependencies=frozenset({Feature.COMPANY_MODEL}),
    ),
    Feature.ORGANIZATIONAL_MEMORY: FeatureSpec(
        feature=Feature.ORGANIZATIONAL_MEMORY,
        description=(
            "Policy-governed long-term Company memory with immutable provenance, "
            "review, supersession, expiry, and retrieval evidence."
        ),
        dependencies=frozenset({Feature.COMPANY_MODEL}),
    ),
    Feature.COMPANY_FINANCE_READ: FeatureSpec(
        feature=Feature.COMPANY_FINANCE_READ,
        description=(
            "Evidence-classified Company economics, budget balances, and finance dashboard."
        ),
        dependencies=frozenset({Feature.COMPANY_MODEL}),
    ),
    Feature.FINANCIAL_GOVERNANCE: FeatureSpec(
        feature=Feature.FINANCIAL_GOVERNANCE,
        description=(
            "Hierarchical budget admission, immutable financial evidence, and "
            "separation-of-duties expense review."
        ),
        dependencies=frozenset({Feature.COMPANY_FINANCE_READ}),
    ),
    Feature.COMPANY_PACKS: FeatureSpec(
        feature=Feature.COMPANY_PACKS,
        description="Declarative, digest-pinned Company Pack preview and installation.",
        dependencies=frozenset(
            {Feature.COMPANY_MODEL, Feature.BUSINESS_OBJECTS}
        ),
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
            Feature.OFFICE_3D,
            Feature.COMPANY_MODEL,
            Feature.COMPANY_GOALS,
            Feature.COMPANY_OPERATIONS,
            Feature.BUSINESS_OBJECTS,
            Feature.ORGANIZATIONAL_MEMORY,
            Feature.COMPANY_FINANCE_READ,
            Feature.FINANCIAL_GOVERNANCE,
            Feature.COMPANY_PACKS,
            Feature.MANAGED_RUNTIME_WORKER,
            Feature.MANAGED_RUNTIME_DIRECT_CUTOVER,
            Feature.DUAL_RECORD_RUNTIME,
            Feature.GENERIC_SUBPROCESS_RUNTIME,
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
