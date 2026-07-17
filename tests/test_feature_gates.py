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

    with pytest.raises(FeatureDisabled, match="agent_registry_management"):
        gates.require(Feature.AGENT_REGISTRY_MANAGEMENT)


def test_profiles_form_an_explicit_capability_ladder() -> None:
    standard = FeatureGateSet.from_config("standard")
    full = FeatureGateSet.from_config("full")

    assert standard.enabled_features == frozenset(
        {Feature.AGENT_REGISTRY_MANAGEMENT, Feature.REVIEWED_EXECUTION}
    )
    assert full.enabled_features == frozenset(Feature)


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
        ("full", "agent_registry_management=false", "requires enabled feature"),
    ],
)
def test_invalid_configuration_fails_fast(profile: str, overrides: str, message: str) -> None:
    with pytest.raises(InvalidFeatureConfiguration, match=message):
        FeatureGateSet.from_config(profile, overrides)
