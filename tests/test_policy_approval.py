import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.services import TaskApplicationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.errors import ExecutionPermitRequired, InvalidPolicyTransition
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import (
    ApprovalOutcome,
    ApprovalStatus,
    GovernedActionType,
    PolicyResult,
)
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory
from tests.test_budget_admission import _reporting_service
from tests.test_task_resolutions import _process_latest

PUBLISHER_TOKEN = "publisher-policy-token-000000000000000000000000"
APPROVER_TOKEN = "approver-policy-token-0000000000000000000000000"
AUTHOR_TOKEN = "author-policy-token-000000000000000000000000000"
OPERATOR_TOKEN = "operator-policy-token-00000000000000000000000000"


def _context(principal_id: str, *roles: Role) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id="test-tenant",
        principal_type=PrincipalType.USER,
        roles=frozenset(roles),
        authenticated=True,
        authentication_method="test",
    )


def _configured(principal_id: str, token: str, roles: list[Role]) -> dict[str, object]:
    return {
        "principal_id": principal_id,
        "tenant_id": "test-tenant",
        "principal_type": "USER",
        "status": "ACTIVE",
        "roles": [role.value for role in roles],
        "token_sha256": sha256(token.encode()).hexdigest(),
    }


def _headers(token: str, permit_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if permit_id is not None:
        headers["Execution-Permit-Id"] = permit_id
    return headers


def test_approval_binds_exact_action_and_permit_consumes_once(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
    )
    publisher = _context("publisher", Role.AGENT_PUBLISHER)
    approver = _context("approver", Role.APPROVER)
    resource_id = uuid4()
    arguments = {"verified_capabilities": ["b", "a", "a"], "make_default": True}
    requested = service.request_action(
        principal=publisher,
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=resource_id,
        arguments=arguments,
        idempotency_key="publish-action-1",
    )
    replayed_request = service.request_action(
        principal=publisher,
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=resource_id,
        arguments={"verified_capabilities": ["a", "b"], "make_default": True},
        idempotency_key="publish-action-1",
    )
    assert replayed_request.action.id == requested.action.id
    assert requested.action.policy_result is PolicyResult.REQUIRE_APPROVAL
    assert requested.action.approval_status is ApprovalStatus.PENDING
    assert requested.action.arguments["verified_capabilities"] == ["a", "b"]
    assert requested.action.approval_id is not None

    with pytest.raises(InvalidPolicyTransition, match="own action"):
        service.decide(
            requested.action.approval_id,
            principal=publisher,
            outcome=ApprovalOutcome.APPROVE,
            reason="Self approve",
        )

    approved = service.decide(
        requested.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Independent review passed",
    )
    replay = service.decide(
        requested.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Independent review passed",
    )
    assert approved.action.permit_id is not None
    assert replay.action.permit_id == approved.action.permit_id

    with pytest.raises(ExecutionPermitRequired, match="does not match"):
        service.consume_permit(
            approved.action.permit_id,
            principal=publisher,
            action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
            resource_type="agent_version",
            resource_id=resource_id,
            arguments={"verified_capabilities": ["a"], "make_default": True},
        )
    service.consume_permit(
        approved.action.permit_id,
        principal=publisher,
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=resource_id,
        arguments=arguments,
    )
    with pytest.raises(InvalidPolicyTransition, match="already consumed"):
        service.consume_permit(
            approved.action.permit_id,
            principal=publisher,
            action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
            resource_type="agent_version",
            resource_id=resource_id,
            arguments=arguments,
        )


def test_policy_supports_allow_deny_and_rejected_approval(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    rules = json.dumps(
        {
            GovernedActionType.AGENT_VERSION_PUBLISH.value: PolicyResult.ALLOW.value,
            GovernedActionType.TASK_BUDGET_INCREASE.value: PolicyResult.DENY.value,
        }
    )
    service = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
        rules_json=rules,
    )
    requester = _context("publisher", Role.AGENT_PUBLISHER)
    allowed = service.request_action(
        principal=requester,
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=uuid4(),
        arguments={},
    )
    denied = service.request_action(
        principal=requester,
        action_type=GovernedActionType.TASK_BUDGET_INCREASE,
        resource_type="task",
        resource_id=uuid4(),
        arguments={"budget": {"max_runs": 3}},
    )
    assert allowed.action.permit_id is not None
    assert allowed.action.approval_status is ApprovalStatus.NOT_REQUIRED
    assert denied.action.policy_result is PolicyResult.DENY
    assert denied.action.permit_id is None


def test_policy_enforces_obligations_quorum_and_ordered_stages(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    rules = json.dumps(
        {
            GovernedActionType.AGENT_VERSION_PUBLISH.value: {
                "result": "REQUIRE_APPROVAL",
                "obligations": {
                    "retain_evidence_days": 365,
                    "reason_required": True,
                },
                "approval_stages": [
                    {
                        "name": "peer-review",
                        "quorum": 2,
                        "eligible_roles": ["APPROVER"],
                    },
                    {
                        "name": "release",
                        "quorum": 1,
                        "eligible_roles": ["TENANT_ADMIN"],
                    },
                ],
            }
        }
    )
    service = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
        rules_json=rules,
    )
    requested = service.request_action(
        principal=_context("publisher", Role.AGENT_PUBLISHER),
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=uuid4(),
        arguments={},
    )
    approval_id = requested.action.approval_id
    assert approval_id is not None
    assert requested.action.obligations["retain_evidence_days"] == 365

    first = service.decide(
        approval_id,
        principal=_context("reviewer-1", Role.APPROVER),
        outcome=ApprovalOutcome.APPROVE,
        reason="First peer review",
    )
    assert first.action.approval_status is ApprovalStatus.PENDING
    assert first.action.current_stage == 0

    second = service.decide(
        approval_id,
        principal=_context("reviewer-2", Role.APPROVER),
        outcome=ApprovalOutcome.APPROVE,
        reason="Second peer review",
    )
    assert second.action.approval_status is ApprovalStatus.PENDING
    assert second.action.current_stage == 1
    assert second.action.permit_id is None

    with pytest.raises(InvalidPolicyTransition, match="TENANT_ADMIN"):
        service.decide(
            approval_id,
            principal=_context("reviewer-3", Role.APPROVER),
            outcome=ApprovalOutcome.APPROVE,
            reason="Wrong role for release",
        )

    released = service.decide(
        approval_id,
        principal=_context("admin", Role.TENANT_ADMIN),
        outcome=ApprovalOutcome.APPROVE,
        reason="Release approved",
    )
    assert released.action.approval_status is ApprovalStatus.APPROVED
    assert released.action.permit_id is not None
    assert [decision.stage for decision in released.decisions] == [
        "peer-review",
        "peer-review",
        "release",
    ]


def test_agent_publish_api_requires_independent_approval(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    principals = [
        _configured("author", AUTHOR_TOKEN, [Role.AGENT_AUTHOR]),
        _configured("publisher", PUBLISHER_TOKEN, [Role.AGENT_PUBLISHER]),
        _configured("approver", APPROVER_TOKEN, [Role.APPROVER]),
    ]
    gates = FeatureGateSet.from_config("full", "identity_rbac=true,policy_approval=true")
    secured = replace(
        application_container,
        feature_gates=gates,
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(principals),
        ),
        policy_service=PolicyApprovalService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            enabled=True,
        ),
    )
    with TestClient(create_app(secured)) as client:
        assert (
            client.post(
                "/api/v1/capabilities",
                headers=_headers(AUTHOR_TOKEN),
                json={
                    "key": "policy.test",
                    "version": "1.0.0",
                    "description": "Policy publication test",
                },
            ).status_code
            == 201
        )
        created = client.post(
            "/api/v1/agents",
            headers=_headers(AUTHOR_TOKEN),
            json={"owner_id": "author", "name": "policy-agent"},
        )
        definition_id = created.json()["id"]
        draft = client.post(
            f"/api/v1/agents/{definition_id}/versions",
            headers=_headers(AUTHOR_TOKEN),
            json={
                "semantic_version": "1.0.0",
                "role": "Policy test",
                "instructions": "Return safe output",
                "declared_capabilities": ["policy.test"],
            },
        )
        version_id = draft.json()["id"]
        client.post(
            f"/api/v1/agent-versions/{version_id}/submit-review",
            headers=_headers(AUTHOR_TOKEN),
        )
        publish_body = {"verified_capabilities": ["policy.test"], "make_default": True}
        missing = client.post(
            f"/api/v1/agent-versions/{version_id}/publish",
            headers=_headers(PUBLISHER_TOKEN),
            json=publish_body,
        )
        assert missing.status_code == 403
        assert missing.json()["code"] == "execution_permit_required"

        intent = client.post(
            "/api/v1/policy/actions",
            headers=_headers(PUBLISHER_TOKEN),
            json={
                "action_type": "agent.version.publish",
                "resource_type": "agent_version",
                "resource_id": version_id,
                "arguments": publish_body,
            },
        ).json()
        approved = client.post(
            f"/api/v1/approvals/{intent['approval_id']}/approve",
            headers=_headers(APPROVER_TOKEN),
            json={"reason": "Independent publication approval"},
        ).json()
        published = client.post(
            f"/api/v1/agent-versions/{version_id}/publish",
            headers=_headers(PUBLISHER_TOKEN, approved["permit_id"]),
            json=publish_body,
        )
        assert published.status_code == 200
        assert published.json()["status"] == "PUBLISHED"
        approvals = client.get(
            "/api/v1/approvals",
            headers=_headers(APPROVER_TOKEN),
        )
        assert approvals.json()["items"][0]["approval_status"] == "CONSUMED"


def test_budget_resume_api_requires_permit_bound_to_replacement(
    application_container: ApplicationContainer,
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task_id = task_service.create_task(
        "Govern a measured budget overrun",
        budget=TaskBudget.create(max_tokens=100, token_reservation_per_attempt=50),
    ).task.id
    task_service.request_run(task_id)
    _process_latest(_reporting_service(uow_factory), uow_factory)

    principals = [
        _configured("operator", OPERATOR_TOKEN, [Role.OPERATOR]),
        _configured("approver", APPROVER_TOKEN, [Role.APPROVER]),
    ]
    gates = FeatureGateSet.from_config("full", "identity_rbac=true,policy_approval=true")
    secured = replace(
        application_container,
        feature_gates=gates,
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(principals),
        ),
        policy_service=PolicyApprovalService(
            uow_factory=uow_factory,
            tenant_id="test-tenant",
            enabled=True,
        ),
    )
    budget = {"max_tokens": 200, "token_reservation_per_attempt": 50}
    with TestClient(create_app(secured)) as client:
        intent = client.post(
            "/api/v1/policy/actions",
            headers=_headers(OPERATOR_TOKEN),
            json={
                "action_type": "task.budget.increase",
                "resource_type": "task",
                "resource_id": str(task_id),
                "arguments": {"budget": budget},
            },
        ).json()
        approved = client.post(
            f"/api/v1/approvals/{intent['approval_id']}/approve",
            headers=_headers(APPROVER_TOKEN),
            json={"reason": "Finance approved the measured overrun"},
        ).json()
        resolved = client.post(
            f"/api/v1/tasks/{task_id}/resolutions/increase-budget-and-resume",
            headers=_headers(OPERATOR_TOKEN, approved["permit_id"]),
            json={
                "actor": "spoofed",
                "reason": "Approved budget increase",
                "budget": budget,
            },
        )
    assert resolved.status_code == 202
    assert resolved.json()["task"]["status"] == "COMPLETED"
    assert resolved.json()["resolution"]["actor"] == "operator"
