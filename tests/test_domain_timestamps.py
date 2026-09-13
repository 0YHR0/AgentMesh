from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.tasks import TaskStatus


def test_message_domain_event_uses_supplied_policy_clock() -> None:
    at = datetime(2031, 2, 3, 4, 5, 6, 789, tzinfo=timezone.utc)
    event = MessageEnvelope.domain_event(
        schema_name="agentmesh.test.event",
        tenant_id="tenant",
        aggregate_id=uuid4(),
        payload={},
        at=at,
    )
    assert event.occurred_at == at


def test_resolution_uses_supplied_policy_clock() -> None:
    at = datetime(2031, 2, 3, 4, 5, 6, 789, tzinfo=timezone.utc)
    resolution = TaskResolution.create(
        task_id=uuid4(),
        action=TaskResolutionAction.REJECT_TASK,
        actor="operator",
        reason="test",
        previous_status=TaskStatus.RUNNING,
        resulting_status=TaskStatus.FAILED,
        previous_error=None,
        at=at,
    )
    assert resolution.created_at == at


@pytest.mark.parametrize("kind", ["event", "resolution"])
def test_supplied_policy_clock_must_be_aware(kind) -> None:
    with pytest.raises((ValueError, InvalidTaskInput)):
        if kind == "event":
            MessageEnvelope.domain_event(
                schema_name="agentmesh.test.event",
                tenant_id="tenant",
                aggregate_id=uuid4(),
                payload={},
                at=datetime(2031, 2, 3),
            )
        else:
            TaskResolution.create(
                task_id=uuid4(),
                action=TaskResolutionAction.REJECT_TASK,
                actor="operator",
                reason="test",
                previous_status=TaskStatus.RUNNING,
                resulting_status=TaskStatus.FAILED,
                previous_error=None,
                at=datetime(2031, 2, 3),
            )
