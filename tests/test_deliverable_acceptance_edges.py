from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from agentmesh.application.deliverable_acceptance import project_deliverable_acceptance
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.deliverable_acceptance import (
    evaluate_checks,
    json_digest,
    normalize_acceptance_policy,
)
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import TaskExecutionMode, TaskStatus


def _rate_policy(**changes):
    check = {
        "key": "ticket-rate",
        "description": "Tickets per buyer stay within the target",
        "kind": "RATE_THRESHOLD",
        "required": True,
        "numerator": {"source": "TASK_INPUT", "path": ["tickets"], "unit": "ticket"},
        "denominator": {"source": "TASK_INPUT", "path": ["buyers"], "unit": "buyer"},
        "scale": 100,
        "operator": "LTE",
        "threshold": 4,
        "tolerance": "0.001",
        **changes,
    }
    return {"checks": [check], "require_human_review": False}


def _normalize(policy):
    return normalize_acceptance_policy(policy, target_subtask_key="main-report")


def _facts(tickets=1200, buyers=10000):
    return {
        "tickets": {"value": tickets, "unit": "ticket"},
        "buyers": {"value": buyers, "unit": "buyer"},
    }


def _evaluate(policy=None, facts=None, output=None):
    return evaluate_checks(
        _normalize(_rate_policy() if policy is None else policy),
        _facts() if facts is None else facts,
        {} if output is None else output,
    )[0]


def _claim_policy(**changes):
    return _rate_policy(
        claim={"source": "DELIVERABLE", "path": ["claimed"], "unit": "ticket/buyer"},
        **changes,
    )


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        [],
        {},
        "abc",
        "NaN",
        "Infinity",
        float("nan"),
        float("inf"),
        float("-inf"),
        "1e19",
        "1e-19",
        "1e1000000",
        "-1e1000000",
        "9" * 65,
    ],
)
@pytest.mark.parametrize("field", ["scale", "threshold", "tolerance"])
def test_rate_policy_rejects_untrusted_numeric_configuration(field, value):
    with pytest.raises(InvalidTaskInput):
        _normalize(_rate_policy(**{field: value}))


@pytest.mark.parametrize(
    "changes",
    [
        {"scale": 0},
        {"scale": -1},
        {"scale": 10001},
        {"threshold": -1},
        {"tolerance": -1},
        {"tolerance": "1.001"},
        {"operator": "LE"},
        {"operator": "lte"},
        {"operator": True},
        {"operator": []},
        {"required": "false"},
        {"required": 1},
        {"kind": "LLM_APPROVES"},
        {"passed": True},
    ],
)
def test_rate_policy_rejects_ambiguous_or_spoofed_options(changes):
    with pytest.raises(InvalidTaskInput):
        _normalize(_rate_policy(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "OUTPUT"},
        {"source": "task_input"},
        {"source": []},
        {"path": "tickets"},
        {"path": []},
        {"path": [1]},
        {"path": [True]},
        {"path": [""]},
        {"path": ["x"] * 17},
        {"path": ["x" * 129]},
        {"path": ["agentmesh_deliverable_acceptance", "checks"]},
        {"unit": ""},
        {"unit": "ticket\x00"},
        {"unit": 1},
        {"unit": "x" * 65},
        {"fallback": 0},
    ],
)
def test_rate_reference_rejects_unknown_sources_bad_paths_and_units(changes):
    policy = _rate_policy()
    policy["checks"][0]["numerator"].update(changes)
    with pytest.raises(InvalidTaskInput):
        _normalize(policy)


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        [],
        {},
        "bad",
        "NaN",
        "Infinity",
        float("nan"),
        float("inf"),
        "1e19",
        "1e-19",
        "1e1000000",
        "-1e1000000",
    ],
)
@pytest.mark.parametrize("field", ["tickets", "buyers"])
def test_invalid_typed_evidence_never_passes_or_becomes_unknown(field, value):
    facts = _facts(100, 10000)
    facts[field]["value"] = value
    assert _evaluate(facts=facts)["status"] == "FAIL"


@pytest.mark.parametrize("field", ["tickets", "buyers"])
@pytest.mark.parametrize("replacement", [1200, "1200", [], {"value": 1200, "unit": "wrong"}])
def test_quantity_types_and_units_are_enforced(field, replacement):
    facts = _facts(100, 10000)
    facts[field] = replacement
    assert _evaluate(facts=facts)["status"] == "FAIL"


@pytest.mark.parametrize("tickets,buyers", [(-1, 10000), (1, 0), (1, -100), (0, 0)])
def test_rate_cannot_use_negative_numerator_or_nonpositive_denominator(tickets, buyers):
    assert _evaluate(facts=_facts(tickets, buyers))["status"] == "FAIL"


@pytest.mark.parametrize("field", ["tickets", "buyers"])
@pytest.mark.parametrize("missing", ["absent", "null", "missing-value", "null-value"])
def test_missing_quantity_values_remain_unknown(field, missing):
    facts = _facts(100, 10000)
    if missing == "absent":
        del facts[field]
    elif missing == "null":
        facts[field] = None
    elif missing == "missing-value":
        del facts[field]["value"]
    else:
        facts[field]["value"] = None
    assert _evaluate(facts=facts)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "operator,threshold,status",
    [
        ("LTE", 12, "PASS"),
        ("GTE", 12, "PASS"),
        ("LTE", "11.999", "FAIL"),
        ("GTE", "12.001", "FAIL"),
    ],
)
def test_rate_threshold_is_inclusive_and_computed_from_evidence(operator, threshold, status):
    result = _evaluate(_rate_policy(operator=operator, threshold=threshold))
    assert result["status"] == status
    assert Decimal(result["evidence"]["calculated"]) == Decimal(12)


@pytest.mark.parametrize("value", [1200, 1200.0, "1200.000"])
def test_finite_decimal_quantity_representations_are_supported(value):
    assert _evaluate(_rate_policy(threshold=12), facts=_facts(value, 10000))["status"] == "PASS"


def test_task_input_reference_does_not_read_deliverable_shadow():
    result = _evaluate(facts={}, output=_facts(1, 10000))
    assert result["status"] == "UNKNOWN"


def test_deliverable_reference_does_not_read_task_input_shadow():
    policy = _rate_policy()
    for field in ("numerator", "denominator"):
        policy["checks"][0][field]["source"] = "DELIVERABLE"
    assert _evaluate(policy, output={})["status"] == "UNKNOWN"


def test_self_reported_acceptance_cannot_spoof_failed_or_missing_evidence():
    spoof = {
        "accepted": True,
        "status": "PASS",
        "checks": [{"passed": True}],
        "acceptance_status": "ACCEPTED",
        "approved_export": True,
    }
    assert _evaluate(output=spoof)["status"] == "FAIL"
    assert _evaluate(facts={}, output=spoof)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "claimed",
    [
        {"value": 4, "unit": "ticket/buyer", "scale": 100},
        {"value": 12, "unit": "percent", "scale": 100},
        {"value": True, "unit": "ticket/buyer", "scale": 100},
        {"value": 12, "unit": "ticket/buyer"},
        {"value": 12, "unit": "ticket/buyer", "scale": True},
        {"value": 12, "unit": "ticket/buyer", "scale": 1},
        {"value": "NaN", "unit": "ticket/buyer", "scale": 100},
    ],
)
def test_claimed_rate_cannot_override_actual_rate_or_quantity_contract(claimed):
    assert _evaluate(_claim_policy(threshold=12), output={"claimed": claimed})["status"] == "FAIL"


def test_claim_cannot_replace_missing_underlying_quantities():
    output = {"claimed": {"value": 1, "unit": "ticket/buyer", "scale": 100}}
    assert _evaluate(_claim_policy(), facts={}, output=output)["status"] == "UNKNOWN"


@pytest.mark.parametrize("value,status", [("12.0005", "PASS"), ("12.002", "FAIL")])
def test_claim_tolerance_checks_recalculated_rate(value, status):
    output = {"claimed": {"value": value, "unit": "ticket/buyer", "scale": 100}}
    assert _evaluate(_claim_policy(threshold=12), output=output)["status"] == status


def test_configured_missing_claim_is_unknown_even_when_actual_rate_passes():
    assert _evaluate(_claim_policy(threshold=12))["status"] == "UNKNOWN"


def test_negative_claim_is_invalid_even_with_permissive_tolerance():
    output = {"claimed": {"value": "-0.1", "unit": "ticket/buyer", "scale": 100}}
    assert (
        _evaluate(_claim_policy(tolerance=1), facts=_facts(0, 100), output=output)["status"]
        == "FAIL"
    )


@pytest.mark.parametrize(
    "expected,actual",
    [
        ({"count": 1}, {"count": True}),
        ([1], [True]),
        ({"count": [1]}, {"count": [1.0]}),
    ],
)
def test_output_equality_does_not_coerce_nested_boolean_or_number_types(expected, actual):
    policy = {
        "checks": [
            {
                "key": "contract",
                "description": "Typed output contract",
                "kind": "OUTPUT_PATH_EQUALS",
                "path": ["payload"],
                "expected": expected,
            }
        ]
    }
    assert _evaluate(policy, output={"payload": actual})["status"] == "FAIL"


@pytest.mark.parametrize("kind", ["OUTPUT_PATH_EXISTS", "OUTPUT_PATH_EQUALS"])
@pytest.mark.parametrize("output", [{}, {"payload": None}, {"parent": {"payload": "ok"}}])
def test_missing_output_field_is_unknown(kind, output):
    check = {"key": "contract", "description": "Output contract", "kind": kind, "path": ["payload"]}
    if kind == "OUTPUT_PATH_EQUALS":
        check["expected"] = "ok"
    assert _evaluate({"checks": [check]}, output=output)["status"] == "UNKNOWN"


def test_claim_reference_cannot_read_task_input_or_use_wrong_ratio_unit():
    for changes in ({"source": "TASK_INPUT"}, {"unit": "buyer/ticket"}):
        policy = _claim_policy()
        policy["checks"][0]["claim"].update(changes)
        with pytest.raises(InvalidTaskInput):
            _normalize(policy)


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "target",
        "unknown",
        "human",
        "empty",
        "duplicate",
        "too-many",
        "only-optional",
        "oversize",
    ],
)
def test_policy_envelope_is_bounded_and_target_pinned(mutation):
    policy = _rate_policy()
    if mutation == "version":
        policy["version"] = True
    elif mutation == "target":
        policy["target_subtask_key"] = "different-report"
    elif mutation == "unknown":
        policy["accepted"] = True
    elif mutation == "human":
        policy["require_human_review"] = "false"
    elif mutation == "empty":
        policy["checks"] = []
    elif mutation == "duplicate":
        policy["checks"] *= 2
    elif mutation == "too-many":
        policy["checks"] = [dict(policy["checks"][0], key=str(i)) for i in range(21)]
    elif mutation == "only-optional":
        policy["checks"][0]["required"] = False
    else:
        policy["checks"] = [
            dict(policy["checks"][0], key=str(i), description="x" * 2000) for i in range(20)
        ]
    with pytest.raises(InvalidTaskInput):
        _normalize(policy)


def test_normalized_policy_does_not_alias_input_and_digest_binds_all_contract_fields():
    supplied = _rate_policy()
    normalized = _normalize(supplied)
    original_digest = json_digest(normalized)
    supplied["checks"][0]["numerator"]["path"].append("tampered")
    supplied["checks"][0]["threshold"] = 100
    assert normalized["checks"][0]["numerator"]["path"] == ["tickets"]
    assert json_digest(normalized) == original_digest
    for field, value in (("threshold", "100"), ("required", False), ("operator", "GTE")):
        changed = deepcopy(normalized)
        changed["checks"][0][field] = value
        assert json_digest(changed) != original_digest


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()])
def test_evidence_digest_rejects_nonfinite_or_non_json_objects(value):
    with pytest.raises(InvalidTaskInput):
        json_digest({"value": value})


def test_digest_is_order_independent_and_distinguishes_bool_from_number():
    assert json_digest({"a": 1, "b": 2}) == json_digest({"b": 2, "a": 1})
    assert json_digest({"a": True}) != json_digest({"a": 1})


@pytest.mark.parametrize("target", [None, "", " ", 1, [], "bad\x00", "\ud800"])
def test_policy_target_must_be_a_bounded_safe_subtask_key(target):
    with pytest.raises(InvalidTaskInput):
        normalize_acceptance_policy(_rate_policy(), target_subtask_key=target)


@pytest.mark.parametrize("location", ["description", "expected"])
def test_policy_rejects_unpaired_unicode_surrogates_without_internal_error(location):
    policy = _rate_policy()
    if location == "description":
        policy["checks"][0]["description"] = "bad\ud800"
    else:
        policy["checks"][0] = {
            "key": "unicode",
            "description": "Safe output",
            "kind": "OUTPUT_PATH_EQUALS",
            "path": ["text"],
            "expected": "bad\ud800",
        }
    with pytest.raises(InvalidTaskInput):
        _normalize(policy)


def test_deeply_nested_equality_contract_fails_without_recursion_error():
    expected = "leaf"
    for _ in range(600):
        expected = [expected]
    policy = {
        "checks": [
            {
                "key": "deep",
                "description": "Bounded JSON contract",
                "kind": "OUTPUT_PATH_EQUALS",
                "path": ["payload"],
                "expected": expected,
            }
        ]
    }
    with pytest.raises(InvalidTaskInput):
        _normalize(policy)


def _completed_task(task_service, uow_factory):
    plan = CoordinatedPlan.create(
        (
            SubtaskSpec.create(key="research", objective="Find evidence"),
            SubtaskSpec.create(
                key="main-report", objective="Write report", depends_on=("research",)
            ),
        ),
        max_concurrency=1,
    )
    aggregate = task_service.create_task(
        "Review a typed business deliverable",
        input=_facts(),
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        acceptance_policy=_rate_policy(),
    )
    task = uow_factory.store.tasks[aggregate.task.id]
    task.status = TaskStatus.COMPLETED
    task.output = {"summary": "Supervisor execution audit"}
    for subtask in uow_factory.store.subtasks.values():
        if subtask.task_id == task.id:
            subtask.status = SubtaskStatus.COMPLETED
            subtask.output = {"summary": "Business report", "claimed": 4}
    acceptance = project_deliverable_acceptance(task_service.get_task(task.id))
    assert acceptance["status"] == "FAILED"
    return task.id, {
        "decision": "ACCEPT",
        "actor": "operator",
        "reason": "Explicit incident override",
        "expected_policy_digest": acceptance["policy_digest"],
        "expected_deliverable_digest": acceptance["deliverable_digest"],
        "idempotency_key": "deliverable-override-edges",
    }


@pytest.mark.parametrize(
    "corruption",
    [
        "task-id",
        "action",
        "actor",
        "reason",
        "policy-digest",
        "deliverable-digest",
        "target",
        "checks",
        "outbox",
        "idempotency-shape",
        "idempotency-id",
    ],
)
def test_deliverable_override_replay_rejects_corrupted_audit(
    task_service,
    resolution_service,
    uow_factory,
    corruption,
):
    task_id, request = _completed_task(task_service, uow_factory)
    result = resolution_service.decide_deliverable(task_id, **request)
    stored = uow_factory.store.task_resolutions[result.resolution.id]
    if corruption == "task-id":
        stored = replace(stored, task_id=uuid4())
    elif corruption == "action":
        stored = replace(stored, action=TaskResolutionAction.REJECT_DELIVERABLE)
    elif corruption in ("actor", "reason"):
        stored = replace(stored, **{corruption: "tampered"})
    elif corruption in ("policy-digest", "deliverable-digest", "target", "checks"):
        key = {
            "policy-digest": "policy_digest",
            "deliverable-digest": "deliverable_digest",
            "target": "target_subtask_key",
            "checks": "checks",
        }[corruption]
        stored = replace(stored, details={**stored.details, key: "tampered"})
    elif corruption == "outbox":
        uow_factory.store.outbox = [
            event for event in uow_factory.store.outbox if event.causation_id != stored.id
        ]
    else:
        identity = next(
            key
            for key, value in uow_factory.store.idempotency.items()
            if value.key == request["idempotency_key"]
        )
        record = uow_factory.store.idempotency[identity]
        value = (
            {**record.result, "unexpected": True}
            if corruption == "idempotency-shape"
            else {"resolution_id": "not-a-uuid"}
        )
        uow_factory.store.idempotency[identity] = replace(record, result=value)
    uow_factory.store.task_resolutions[stored.id] = stored
    before = deepcopy(uow_factory.store)
    with pytest.raises(InvalidTaskTransition):
        resolution_service.decide_deliverable(task_id, **request)
    query_counters = {"run_list_for_task_calls", "attempt_list_for_task_calls"}
    assert {
        key: value for key, value in vars(uow_factory.store).items() if key not in query_counters
    } == {key: value for key, value in vars(before).items() if key not in query_counters}


@pytest.mark.parametrize("corruption", ["policy", "output", "input"])
def test_human_override_never_survives_changed_policy_deliverable_or_evidence(
    task_service,
    resolution_service,
    uow_factory,
    corruption,
):
    task_id, request = _completed_task(task_service, uow_factory)
    resolution_service.decide_deliverable(task_id, **request)
    assert project_deliverable_acceptance(task_service.get_task(task_id))["delivery_allowed"]
    task = uow_factory.store.tasks[task_id]
    if corruption == "policy":
        task.input["agentmesh_deliverable_acceptance"]["checks"][0]["threshold"] = "3"
    elif corruption == "input":
        task.input["tickets"]["value"] = 1300
    else:
        subtask = next(
            item
            for item in uow_factory.store.subtasks.values()
            if item.task_id == task_id and item.key == "main-report"
        )
        subtask.output["summary"] = "Replaced report"
    acceptance = project_deliverable_acceptance(task_service.get_task(task_id))
    assert acceptance["status"] == "FAILED"
    assert acceptance["human_decision"] is None
    assert acceptance["delivery_allowed"] is False
    with pytest.raises(InvalidTaskTransition):
        resolution_service.decide_deliverable(task_id, **request)


def test_foreign_task_decision_cannot_authorize_identical_business_evidence(
    task_service,
    resolution_service,
    uow_factory,
):
    task_id, request = _completed_task(task_service, uow_factory)
    result = resolution_service.decide_deliverable(task_id, **request)
    aggregate = replace(
        task_service.get_task(task_id),
        deliverable_decisions=[replace(result.resolution, task_id=uuid4())],
    )
    acceptance = project_deliverable_acceptance(aggregate)
    assert acceptance["status"] == "FAILED"
    assert acceptance["delivery_allowed"] is False
    assert acceptance["human_decision"] is None
