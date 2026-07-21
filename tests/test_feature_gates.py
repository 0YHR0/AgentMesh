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

    with pytest.raises(FeatureDisabled, match="agent_registry_management"):
        gates.require(Feature.AGENT_REGISTRY_MANAGEMENT)


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
                Feature.MCP_WRITE_TOOLS,
            Feature.A2A_FEDERATION,
            Feature.A2A_DELEGATION,
            Feature.A2A_RECONCILIATION,
            Feature.OUTCOME_RECONCILIATION,
            Feature.CREDENTIAL_BROKER,
        }
    )
    assert Feature.IDENTITY_RBAC not in full.enabled_features


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
