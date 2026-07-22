from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidAgentTransition, InvalidAgentVersion
from agentmesh.domain.registry import AgentVersion, AgentVersionStatus
from agentmesh.domain.tasks import utc_now


def make_version() -> AgentVersion:
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


def test_agent_version_canonicalizes_digest_bound_runtime_policies() -> None:
    credential_id = uuid4()
    version = AgentVersion.create_draft(
        definition_id=uuid4(),
        semantic_version="1.0.0",
        role="Researcher",
        instructions="Use evidence.",
        declared_capabilities=["general.task"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        model_policy={
            "provider": "OPENAI",
            "model": " gpt-test ",
            "reasoning_effort": "LOW",
            "max_output_tokens": 512,
            "credential_reference_id": str(credential_id),
        },
        tool_profile={
            "allowed_tools": ["workspace.read", "workspace.read"],
            "max_calls": 2,
        },
    )

    assert version.model_policy == {
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_effort": "low",
        "max_output_tokens": 512,
        "credential_reference_id": str(credential_id),
    }
    assert version.tool_profile == {
        "allowed_tools": ["workspace.read"],
        "max_calls": 2,
    }


def test_agent_version_rejects_unbounded_or_unknown_runtime_policy() -> None:
    values = {
        "definition_id": uuid4(),
        "semantic_version": "1.0.0",
        "role": "Researcher",
        "instructions": "Use evidence.",
        "declared_capabilities": ["general.task"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }
    with pytest.raises(InvalidAgentVersion, match="Unknown model_policy"):
        AgentVersion.create_draft(
            **values,
            model_policy={"provider": "openai", "api_key": "must-not-be-stored"},
        )
    with pytest.raises(InvalidAgentVersion, match="between 1 and 8"):
        AgentVersion.create_draft(
            **values,
            tool_profile={"allowed_tools": ["workspace.read"], "max_calls": 9},
        )
