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
        set(Feature) - {Feature.IDENTITY_RBAC, Feature.PERSISTENT_IDENTITY, Feature.POLICY_APPROVAL}
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
