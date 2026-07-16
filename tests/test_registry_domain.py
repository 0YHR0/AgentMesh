import pytest

from agentmesh.domain.errors import InvalidAgentTransition, InvalidAgentVersion
from agentmesh.domain.registry import AgentVersion, AgentVersionStatus
from agentmesh.domain.tasks import utc_now


def make_version() -> AgentVersion:
    from uuid import uuid4

    return AgentVersion.create_draft(
        definition_id=uuid4(),
        semantic_version="1.2.3",
        role="Python reviewer",
        instructions="Review Python code and return findings.",
        declared_capabilities=["code.review.python", "code.review.security"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        runtime_adapter="local-python",
        execution_modes=["async"],
    )


def test_agent_version_publish_lifecycle_and_digest() -> None:
    version = make_version()
    version.submit_for_review()
    version.publish(["code.review.python"])

    assert version.status == AgentVersionStatus.PUBLISHED
    assert version.content_digest is not None
    assert version.content_digest.startswith("sha256:")
    assert version.published_at is not None

    with pytest.raises(InvalidAgentTransition):
        version.publish(["code.review.python"])


def test_verified_capability_must_be_declared() -> None:
    version = make_version()
    version.submit_for_review()

    with pytest.raises(InvalidAgentVersion):
        version.publish(["code.write.python"])


def test_revocation_requires_reason() -> None:
    version = make_version()
    version.submit_for_review()
    version.publish([])

    with pytest.raises(InvalidAgentVersion):
        version.revoke("  ")

    assert version.updated_at <= utc_now()
