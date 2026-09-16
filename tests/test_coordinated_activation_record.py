"""Drift guards for the c2f8 coordinated activation record.

The activation record is deliberately data-only.  These tests bind it to the
runtime's source-of-truth feature profiles, startup restrictions, and Alembic
script graph so a later migration or gate change cannot silently make the
qualification record stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from agentmesh.bootstrap import _validate_managed_cutover_config
from agentmesh.config import Settings
from agentmesh.domain.errors import InvalidFeatureConfiguration
from agentmesh.features import (
    PROFILE_FEATURES,
    Feature,
    FeatureGateSet,
    FeatureProfile,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
RECORD_PATH = REPOSITORY_ROOT / "docs" / "qualification" / (
    "a4-2-coordinated-activation.json"
)


@pytest.fixture(scope="module")
def activation_record() -> dict[str, object]:
    return json.loads(RECORD_PATH.read_text(encoding="utf-8"))


def test_activation_record_has_closed_machine_readable_shape(
    activation_record: dict[str, object],
) -> None:
    assert activation_record["schema"] == "agentmesh.qualification.a4-2-coordinated-activation.v1"
    assert activation_record["milestone"] == "A4.2c.2f8"
    assert activation_record["status"] == "pending_activation"
    assert activation_record["parity"] == {
        "status": "pending",
        "report": None,
        "production_admission": False,
    }
    assert activation_record["feature_gate"] == {
        "name": Feature.MANAGED_RUNTIME_COORDINATED_CUTOVER.value,
        "profiles": {profile.value: False for profile in FeatureProfile},
        "production_admission": False,
    }
    assert activation_record["activation_constraints"] == {
        "allowed_environments": ["test", "testing"],
        "provider": "deterministic",
        "production_admission": False,
    }
    assert [window["id"] for window in activation_record["crash_windows"]] == [
        "claim_commit_before_inspect_provider",
        "prepare_commit_before_dispatch",
        "boundary_cas_before_provider",
        "provider_response_loss_before_receipt_bind",
        "terminal_evidence_business_commit",
        "deadline_claim_finalize",
    ]


def test_activation_record_matches_actual_alembic_graph(
    activation_record: dict[str, object],
) -> None:
    script = ScriptDirectory.from_config(Config(str(REPOSITORY_ROOT / "alembic.ini")))
    revisions = {revision.revision for revision in script.walk_revisions()}

    assert activation_record["migration_head"] == script.get_current_head()
    assert activation_record["downgrade_floor"] in revisions
    assert activation_record["downgrade_floor"] == activation_record["migration_head"]


def test_activation_record_matches_all_shipped_profile_defaults(
    activation_record: dict[str, object],
) -> None:
    feature = Feature.MANAGED_RUNTIME_COORDINATED_CUTOVER
    recorded_profiles = activation_record["feature_gate"]["profiles"]

    assert set(recorded_profiles) == {profile.value for profile in FeatureProfile}
    for profile in FeatureProfile:
        gates = FeatureGateSet.from_config(profile.value)
        assert gates.enabled_features == PROFILE_FEATURES[profile]
        assert gates.is_enabled(feature) is recorded_profiles[profile.value]
        assert recorded_profiles[profile.value] is False


def test_activation_record_matches_cutover_startup_validation(
    activation_record: dict[str, object],
) -> None:
    constraints = activation_record["activation_constraints"]
    gates = FeatureGateSet.from_config(
        "full",
        "managed_runtime_worker=true,managed_runtime_coordinated_cutover=true",
    )

    for environment in constraints["allowed_environments"]:
        _validate_managed_cutover_config(
            Settings(environment=environment, model_provider=constraints["provider"]),
            gates,
        )

    with pytest.raises(InvalidFeatureConfiguration, match="CI/test-only"):
        _validate_managed_cutover_config(
            Settings(environment="production", model_provider=constraints["provider"]),
            gates,
        )
    with pytest.raises(InvalidFeatureConfiguration, match="deterministic model provider"):
        _validate_managed_cutover_config(
            Settings(environment="testing", model_provider="openai"),
            gates,
        )
