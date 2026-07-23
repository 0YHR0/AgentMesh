import json
from pathlib import Path
from uuid import UUID

from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.policy import GovernedActionType, canonical_action_hash

FIXTURE = Path(__file__).parent / "contract_fixtures" / "v1_contracts.json"


def test_v1_message_envelope_fixture_remains_readable() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["message_envelope"]

    restored = MessageEnvelope.from_dict(fixture)
    serialized = restored.to_dict()

    assert serialized == {
        key: value for key, value in fixture.items() if key != "forward_compatible_extension"
    }


def test_v1_action_intent_hash_remains_stable() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["action_intent"]

    actual = canonical_action_hash(
        tenant_id=fixture["tenant_id"],
        requester_id=fixture["requester_id"],
        action_type=GovernedActionType(fixture["action_type"]),
        resource_type=fixture["resource_type"],
        resource_id=UUID(fixture["resource_id"]),
        arguments=fixture["arguments"],
    )

    assert actual == fixture["canonical_hash"]
