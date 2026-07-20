from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidPolicyTransition


class GovernedActionType(str, Enum):
    AGENT_VERSION_PUBLISH = "agent.version.publish"
    TASK_BUDGET_INCREASE = "task.budget.increase"
    MCP_SERVER_VERSION_PUBLISH = "mcp.server-version.publish"
    A2A_DELEGATE = "a2a.delegate"
    CREDENTIAL_BINDING_CREATE = "credential.binding.create"


class PolicyResult(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class ApprovalOutcome(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


CANONICALIZATION_VERSION = "agentmesh-action-v1"


def canonical_action_hash(
    *,
    tenant_id: str,
    requester_id: str,
    action_type: GovernedActionType,
    resource_type: str,
    resource_id: UUID,
    arguments: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "canonicalization_version": CANONICALIZATION_VERSION,
            "tenant_id": tenant_id,
            "requester_id": requester_id,
            "action_type": action_type.value,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "arguments": arguments,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class GovernedAction:
    id: UUID
    tenant_id: str
    requester_id: str
    action_type: GovernedActionType
    resource_type: str
    resource_id: UUID
    arguments: dict[str, Any]
    canonicalization_version: str
    action_hash: str
    policy_result: PolicyResult
    reason_code: str
    policy_bundle: str
    policy_version: str
    approval_id: UUID | None
    approval_status: ApprovalStatus
    permit_id: UUID | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        requester_id: str,
        action_type: GovernedActionType,
        resource_type: str,
        resource_id: UUID,
        arguments: dict[str, Any],
        policy_result: PolicyResult,
        reason_code: str,
        policy_bundle: str,
        policy_version: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> GovernedAction:
        approval_id = uuid4() if policy_result is PolicyResult.REQUIRE_APPROVAL else None
        permit_id = uuid4() if policy_result is PolicyResult.ALLOW else None
        status = (
            ApprovalStatus.PENDING
            if policy_result is PolicyResult.REQUIRE_APPROVAL
            else ApprovalStatus.NOT_REQUIRED
        )
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            requester_id=requester_id,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            arguments=dict(arguments),
            canonicalization_version=CANONICALIZATION_VERSION,
            action_hash=canonical_action_hash(
                tenant_id=tenant_id,
                requester_id=requester_id,
                action_type=action_type,
                resource_type=resource_type,
                resource_id=resource_id,
                arguments=arguments,
            ),
            policy_result=policy_result,
            reason_code=reason_code,
            policy_bundle=policy_bundle,
            policy_version=policy_version,
            approval_id=approval_id,
            approval_status=status,
            permit_id=permit_id,
            created_at=created_at,
            expires_at=expires_at,
        )

    def decide(
        self,
        *,
        approver_id: str,
        outcome: ApprovalOutcome,
        now: datetime,
    ) -> GovernedAction:
        if approver_id == self.requester_id:
            raise InvalidPolicyTransition("Requester cannot approve or reject their own action")
        if self.approval_status is not ApprovalStatus.PENDING:
            raise InvalidPolicyTransition("Approval is no longer pending")
        if now >= self.expires_at:
            return replace(self, approval_status=ApprovalStatus.EXPIRED, revision=self.revision + 1)
        if outcome is ApprovalOutcome.REJECT:
            return replace(
                self,
                approval_status=ApprovalStatus.REJECTED,
                decided_at=now,
                revision=self.revision + 1,
            )
        return replace(
            self,
            approval_status=ApprovalStatus.APPROVED,
            permit_id=uuid4(),
            decided_at=now,
            revision=self.revision + 1,
        )

    def consume(self, *, now: datetime) -> GovernedAction:
        if self.permit_id is None:
            raise InvalidPolicyTransition("Action has no execution Permit")
        if self.consumed_at is not None or self.approval_status is ApprovalStatus.CONSUMED:
            raise InvalidPolicyTransition("Execution Permit was already consumed")
        if now >= self.expires_at:
            raise InvalidPolicyTransition("Execution Permit has expired")
        if self.policy_result is PolicyResult.DENY:
            raise InvalidPolicyTransition("Denied action cannot be executed")
        if self.policy_result is PolicyResult.REQUIRE_APPROVAL and (
            self.approval_status is not ApprovalStatus.APPROVED
        ):
            raise InvalidPolicyTransition("Action has not been approved")
        return replace(
            self,
            approval_status=ApprovalStatus.CONSUMED,
            consumed_at=now,
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class ApprovalDecision:
    id: UUID
    governed_action_id: UUID
    approval_id: UUID
    approver_id: str
    outcome: ApprovalOutcome
    reason: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        governed_action_id: UUID,
        approval_id: UUID,
        approver_id: str,
        outcome: ApprovalOutcome,
        reason: str,
        created_at: datetime,
    ) -> ApprovalDecision:
        if not reason.strip():
            raise InvalidPolicyTransition("Approval decision reason must not be blank")
        return cls(
            id=uuid4(),
            governed_action_id=governed_action_id,
            approval_id=approval_id,
            approver_id=approver_id,
            outcome=outcome,
            reason=reason.strip(),
            created_at=created_at.astimezone(timezone.utc),
        )
