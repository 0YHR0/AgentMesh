import json

from agentmesh.orchestration.model_agent import OpenAIResponsesAgentExecutor


def test_pinned_employee_schema_preserves_business_roots():
    result = OpenAIResponsesAgentExecutor._structured_result(
        json.dumps({"summary": "Draft", "lyrics": "Original words", "ignored": "not declared"}),
        output_schema={"type": "object", "properties": {"lyrics": {"type": "string"}}},
    )
    assert result == {"summary": "Draft", "lyrics": "Original words"}


def test_pinned_schema_cannot_replace_platform_identity_or_execution():
    result = OpenAIResponsesAgentExecutor._structured_result(
        json.dumps({"summary": "Plan", "agent": "spoof", "execution": "spoof",
                    "composition": {"tempo": 100}}),
        output_schema={"properties": {"agent": {}, "execution": {}, "composition": {}}},
    )
    assert result == {"summary": "Plan", "composition": {"tempo": 100}}


def test_default_contract_does_not_keep_arbitrary_model_fields():
    result = OpenAIResponsesAgentExecutor._structured_result(
        '{"summary":"ok","lyrics":"undeclared"}', output_schema={"type": "object"},
    )
    assert result == {"summary": "ok"}


def test_existing_acceptance_roots_and_pinned_schema_can_coexist():
    result = OpenAIResponsesAgentExecutor._structured_result(
        '{"summary":"ok","claim":9,"lyrics":"original"}',
        acceptance_contract={"version": 1, "output_roots": ["claim"]},
        output_schema={"properties": {"lyrics": {}}},
    )
    assert result == {"summary": "ok", "claim": 9, "lyrics": "original"}


def test_malformed_json_remains_prose_without_invented_fields():
    result = OpenAIResponsesAgentExecutor._structured_result(
        "unfinished {", output_schema={"properties": {"lyrics": {}}},
    )
    assert result == {"summary": "unfinished {"}
