import pytest

from agentmesh.domain.errors import FeatureDisabled, InvalidFeatureConfiguration
from agentmesh.features import FEATURE_SPECS, Feature, FeatureGateSet, FeatureProfile


def test_every_declared_feature_has_a_specification() -> None:
    assert set(FEATURE_SPECS) == set(Feature)


def test_minimal_profile_disables_all_optional_features() -> None:
    gates = FeatureGateSet.from_config("minimal")

    assert gates.profile is FeatureProfile.MINIMAL
    assert not gates.is_enabled(Feature.AGENT_REGISTRY_MANAGEMENT)
    assert not gates.is_enabled(Feature.AGENT_DEPLOYMENTS)
    assert not gates.is_enabled(Feature.MCP_READ_TOOLS)
    assert not gates.is_enabled(Feature.OBSERVABILITY)
    assert not gates.is_enabled(Feature.BUDGET_ADMISSION)
    assert not gates.is_enabled(Feature.REALTIME_EVENTS)
    assert not gates.is_enabled(Feature.ACTIVITY_TIMELINE)
    assert not gates.is_enabled(Feature.OFFICE_3D)
    assert not gates.is_enabled(Feature.COMPANY_MODEL)
    assert not gates.is_enabled(Feature.COMPANY_GOALS)
    assert not gates.is_enabled(Feature.COMPANY_OPERATIONS)
    assert not gates.is_enabled(Feature.BUSINESS_OBJECTS)
    assert not gates.is_enabled(Feature.ORGANIZATIONAL_MEMORY)

    with pytest.raises(FeatureDisabled, match="agent_registry_management"):
        gates.require(Feature.AGENT_REGISTRY_MANAGEMENT)


def test_managed_shadow_admission_requires_both_worker_and_dual_record_gates() -> None:
    gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true,managed_runtime_worker=true"
    )
    gates.require(Feature.MANAGED_RUNTIME_WORKER)
    with pytest.raises(FeatureDisabled, match="dual_record_runtime"):
        gates.require(Feature.DUAL_RECORD_RUNTIME)


def test_profiles_form_an_explicit_capability_ladder() -> None:
    standard = FeatureGateSet.from_config("standard")
    full = FeatureGateSet.from_config("full")

    assert standard.enabled_features == frozenset(
        {
            Feature.AGENT_REGISTRY_MANAGEMENT,
            Feature.REVIEWED_EXECUTION,
            Feature.HUMAN_RESOLUTION,
        }
    )
    assert full.enabled_features == frozenset(
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
            Feature.DUAL_RECORD_RUNTIME,
        }
    )
    assert Feature.IDENTITY_RBAC not in full.enabled_features
    assert Feature.REALTIME_EVENTS in full.enabled_features
    assert Feature.ACTIVITY_TIMELINE in full.enabled_features
    assert Feature.MANAGED_RUNTIME_WORKER not in full.enabled_features
    assert Feature.DUAL_RECORD_RUNTIME not in full.enabled_features
    assert Feature.OFFICE_3D not in full.enabled_features
    assert Feature.COMPANY_MODEL not in full.enabled_features
    assert Feature.COMPANY_GOALS not in full.enabled_features
    assert Feature.COMPANY_OPERATIONS not in full.enabled_features
    assert Feature.BUSINESS_OBJECTS not in full.enabled_features
    assert Feature.ORGANIZATIONAL_MEMORY not in full.enabled_features


def test_company_model_is_explicit_and_requires_agent_registry() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="agent_registry_management"):
        FeatureGateSet.from_config("minimal", "company_model=true")

    enabled = FeatureGateSet.from_config("full", "company_model=true")
    assert enabled.is_enabled(Feature.COMPANY_MODEL)


def test_company_goals_requires_company_model() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="company_model"):
        FeatureGateSet.from_config("full", "company_goals=true")

    enabled = FeatureGateSet.from_config(
        "full", "company_model=true,company_goals=true"
    )
    assert enabled.is_enabled(Feature.COMPANY_GOALS)


def test_company_operations_requires_company_goals() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="company_goals"):
        FeatureGateSet.from_config(
            "full", "company_model=true,company_operations=true"
        )

    enabled = FeatureGateSet.from_config(
        "full",
        "company_model=true,company_goals=true,company_operations=true",
    )
    assert enabled.is_enabled(Feature.COMPANY_OPERATIONS)


def test_business_objects_require_company_model_but_not_operations() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="company_model"):
        FeatureGateSet.from_config("full", "business_objects=true")

    enabled = FeatureGateSet.from_config(
        "full", "company_model=true,business_objects=true"
    )
    assert enabled.is_enabled(Feature.BUSINESS_OBJECTS)
    assert not enabled.is_enabled(Feature.COMPANY_OPERATIONS)


def test_organizational_memory_requires_company_model() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="company_model"):
        FeatureGateSet.from_config("full", "organizational_memory=true")

    enabled = FeatureGateSet.from_config(
        "full", "company_model=true,organizational_memory=true"
    )
    assert enabled.is_enabled(Feature.ORGANIZATIONAL_MEMORY)


def test_identity_is_an_explicit_opt_in_even_for_full_profile() -> None:
    enabled = FeatureGateSet.from_config("full", "identity_rbac=true")

    assert enabled.is_enabled(Feature.IDENTITY_RBAC)


def test_policy_requires_explicit_identity_dependency() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="identity_rbac"):
        FeatureGateSet.from_config("full", "policy_approval=true")

    enabled = FeatureGateSet.from_config("full", "identity_rbac=true,policy_approval=true")
    assert enabled.is_enabled(Feature.POLICY_APPROVAL)


def test_governed_mcp_requires_read_tools_identity_and_policy() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="requires enabled feature"):
        FeatureGateSet.from_config("minimal", "governed_mcp=true")
    enabled = FeatureGateSet.from_config(
        "minimal",
        "mcp_read_tools=true,identity_rbac=true,policy_approval=true,governed_mcp=true",
    )
    assert enabled.is_enabled(Feature.GOVERNED_MCP)


def test_a2a_federation_requires_explicit_identity() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="identity_rbac"):
        FeatureGateSet.from_config("minimal", "a2a_federation=true")
    enabled = FeatureGateSet.from_config("minimal", "identity_rbac=true,a2a_federation=true")
    assert enabled.is_enabled(Feature.A2A_FEDERATION)


def test_a2a_delegation_requires_registry_identity_and_policy() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="requires enabled feature"):
        FeatureGateSet.from_config("minimal", "a2a_delegation=true")
    enabled = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true",
    )
    assert enabled.is_enabled(Feature.A2A_DELEGATION)


def test_a2a_reconciliation_requires_delegation() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="requires enabled feature"):
        FeatureGateSet.from_config("minimal", "a2a_reconciliation=true")
    enabled = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,policy_approval=true,a2a_federation=true,"
        "a2a_delegation=true,a2a_reconciliation=true",
    )
    assert enabled.is_enabled(Feature.A2A_RECONCILIATION)


def test_outcome_reconciliation_requires_identity_and_resolution_ledger() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="requires enabled feature"):
        FeatureGateSet.from_config("minimal", "outcome_reconciliation=true")
    enabled = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,human_resolution=true,outcome_reconciliation=true",
    )
    assert enabled.is_enabled(Feature.OUTCOME_RECONCILIATION)


def test_credential_broker_requires_persistent_identity_and_policy() -> None:
    with pytest.raises(InvalidFeatureConfiguration, match="requires enabled feature"):
        FeatureGateSet.from_config("minimal", "credential_broker=true")
    enabled = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,persistent_identity=true,policy_approval=true,credential_broker=true",
    )
    assert enabled.is_enabled(Feature.CREDENTIAL_BROKER)


def test_explicit_overrides_are_applied_after_profile() -> None:
    gates = FeatureGateSet.from_config(
        "minimal",
        "agent_registry_management=true,agent_deployments=true",
    )

    assert gates.enabled_features == frozenset(
        {Feature.AGENT_REGISTRY_MANAGEMENT, Feature.AGENT_DEPLOYMENTS}
    )


@pytest.mark.parametrize(
    ("profile", "overrides", "message"),
    [
        ("unknown", "", "Unknown feature profile"),
        ("minimal", "missing-separator", "Invalid feature override"),
        ("minimal", "unknown=true", "Unknown feature"),
        ("minimal", "agent_registry_management=yes", "expected true or false"),
        (
            "minimal",
            "agent_registry_management=true,agent_registry_management=false",
            "configured more than once",
        ),
        ("minimal", "agent_deployments=true", "requires enabled feature"),
        ("minimal", "handoffs=true", "requires enabled feature"),
        ("minimal", "budget_admission=true", "requires enabled feature"),
        ("full", "agent_registry_management=false", "requires enabled feature"),
    ],
)
def test_invalid_configuration_fails_fast(profile: str, overrides: str, message: str) -> None:
    with pytest.raises(InvalidFeatureConfiguration, match=message):
        FeatureGateSet.from_config(profile, overrides)
