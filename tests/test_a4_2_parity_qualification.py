"""Drift guards for the publishable A4.2d parity qualification report."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_managed_coordinated_parity import COORDINATED_PARITY_FIXTURE_REPORT
from tests.test_managed_reviewed_parity import PARITY_FIXTURE_REPORT

REPOSITORY_ROOT = Path(__file__).parents[1]
REPORT_PATH = REPOSITORY_ROOT / "docs" / "qualification" / "a4-2-parity.json"


def _node_exists(node: str) -> None:
    source, test_name = node.split("::", 1)
    path = REPOSITORY_ROOT / source
    assert path.is_file(), node
    function_name = test_name.split("[", 1)[0]
    assert f"def {function_name}" in path.read_text(encoding="utf-8"), node


def test_a4_2_parity_report_matches_reviewed_and_coordinated_fixture_sources() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema"] == "agentmesh.qualification.a4-2-parity.v1"
    assert report["milestone"] == "A4.2d"
    assert report["status"] == "qualified_with_documented_exceptions"
    assert report["production_admission"] is False
    assert report["fixtures"]["reviewed"] == list(PARITY_FIXTURE_REPORT)
    assert report["fixtures"]["coordinated"] == list(COORDINATED_PARITY_FIXTURE_REPORT)

    coordinated = list(COORDINATED_PARITY_FIXTURE_REPORT)
    exact = {
        entry["scenario"] for entry in coordinated if entry["parity_pass"] is True
    }
    exceptions = {
        entry["scenario"] for entry in coordinated if entry["parity_pass"] is False
    }
    assert set(report["exact_equality"]["coordinated_scenarios"]) == exact
    assert set(report["exact_equality"]["excluded_from_exact_equality"]) == exceptions
    assert exact == {"parallel_join_success"}
    assert exceptions == {
        "executor_failure",
        "cancel_requested",
        "budget_wait_resume",
        "unknown_outcome",
    }
    assert {
        item["scenario"] for item in report["documented_safety_exceptions"]
    } == exceptions
    assert all(
        item["parity_pass"] is False
        for item in report["documented_safety_exceptions"]
    )


def test_a4_2_parity_report_references_capability_qualified_runtime_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["runtime_conformance"]["status"] == "qualified"
    adapters = {
        evidence["adapter"]
        for evidence in report["runtime_conformance"]["evidence"]
    }
    assert adapters == {"langgraph", "subprocess"}
    for evidence in report["runtime_conformance"]["evidence"]:
        for node in evidence["test_nodes"]:
            _node_exists(node)

    for node in report["managed_business_behavior_evidence"]:
        _node_exists(node)
    managed_nodes = report["managed_business_behavior_evidence"]
    assert any("[cancel_requested]" in node for node in managed_nodes)
    assert any("[unknown_outcome]" in node for node in managed_nodes)
    assert any("[budget_wait_resume]" in node for node in managed_nodes)
    assert any(
        "control_postgres.py::test_postgres_cancel_crossed" in node
        for node in managed_nodes
    )
    assert any(
        "unknown_postgres.py::test_postgres_executor_unknown_parks" in node
        for node in managed_nodes
    )
    assert any("budget_resume_postgres.py::test_postgres_managed" in node for node in managed_nodes)
