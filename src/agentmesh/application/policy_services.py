from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.errors import (
    ExecutionPermitRequired,
    GovernedActionNotFound,
    IdempotencyConflict,
    InvalidPolicyConfiguration,
    InvalidPolicyTransition,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.policy import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalStatus,
    GovernedAction,
    GovernedActionType,
    PolicyResult,
    canonical_action_hash,
)

DEFAULT_POLICY_RULES = json.dumps(
    {
        GovernedActionType.AGENT_VERSION_PUBLISH.value: PolicyResult.REQUIRE_APPROVAL.value,
        GovernedActionType.TASK_BUDGET_INCREASE.value: PolicyResult.REQUIRE_APPROVAL.value,
    }
)


@dataclass(frozen=True)
class GovernedActionResult:
    action: GovernedAction
    decisions: tuple[ApprovalDecision, ...]


class PolicyApprovalService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        enabled: bool,
        rules_json: str = DEFAULT_POLICY_RULES,
        policy_bundle: str = "agentmesh-builtin",
        policy_version: str = "1.0.0",
        ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self.enabled = enabled
        self._rules = self._parse_rules(rules_json) if enabled else {}
        self._policy_bundle = policy_bundle.strip()
        self._policy_version = policy_version.strip()
        self._ttl = ttl
        if enabled and (not self._policy_bundle or not self._policy_version):
            raise InvalidPolicyConfiguration("Policy bundle and version must not be blank")
        if enabled and (ttl <= timedelta(0) or ttl > timedelta(hours=24)):
            raise InvalidPolicyConfiguration(
                "Policy action TTL must be between 1 second and 24 hours"
            )

    def request_action(
        self,
        *,
        principal: PrincipalContext,
        action_type: GovernedActionType,
        resource_type: str,
        resource_id: UUID,
        arguments: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> GovernedActionResult:
        self._require_principal(principal)
        normalized = self.normalize_arguments(action_type, arguments)
        result = self._rules.get(action_type, PolicyResult.DENY)
        now = datetime.now(timezone.utc)
        action = GovernedAction.create(
            tenant_id=self._tenant_id,
            requester_id=principal.principal_id,
            action_type=action_type,
            resource_type=resource_type.strip(),
            resource_id=resource_id,
            arguments=normalized,
            policy_result=result,
            reason_code=f"builtin.{result.value.lower()}",
            policy_bundle=self._policy_bundle,
            policy_version=self._policy_version,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._uow_factory() as uow:
            key = (idempotency_key or "").strip()
            if idempotency_key is not None and not key:
                raise InvalidPolicyTransition("Idempotency-Key must not be blank")
            scope = f"policy-action:{self._tenant_id}:{principal.principal_id}"
            if key:
                uow.idempotency.lock(scope, key)
                existing = uow.idempotency.get(scope, key)
                if existing is not None:
                    if existing.request_hash != action.action_hash:
                        raise IdempotencyConflict(
                            f"Idempotency key '{key}' was reused for another governed action"
                        )
                    replay = uow.policy.get_action(UUID(str(existing.result["action_id"])))
                    if replay is None:
                        raise InvalidPolicyTransition("Governed action idempotency result was lost")
                    return GovernedActionResult(
                        replay,
                        tuple(uow.policy.list_decisions(replay.id)),
                    )
            uow.policy.add_action(action)
            if key:
                uow.idempotency.add(
                    IdempotencyRecord.create(
                        scope=scope,
                        key=key,
                        request_hash=action.action_hash,
                        result={"action_id": str(action.id)},
                    )
                )
            self._event(uow, action, "agentmesh.policy.action-requested")
            uow.commit()
        return GovernedActionResult(action, ())

    def decide(
        self,
        approval_id: UUID,
        *,
        principal: PrincipalContext,
        outcome: ApprovalOutcome,
        reason: str,
    ) -> GovernedActionResult:
        self._require_principal(principal)
        with self._uow_factory() as uow:
            action = uow.policy.get_by_approval(approval_id, for_update=True)
            if action is None or action.tenant_id != self._tenant_id:
                raise GovernedActionNotFound(f"Approval {approval_id} was not found")
            existing = uow.policy.list_decisions(action.id)
            if existing:
                decision = existing[-1]
                if (
                    decision.approver_id == principal.principal_id
                    and decision.outcome is outcome
                    and decision.reason == reason.strip()
                ):
                    return GovernedActionResult(action, tuple(existing))
                raise InvalidPolicyTransition("Approval already has a different decision")
            now = datetime.now(timezone.utc)
            updated = action.decide(
                approver_id=principal.principal_id,
                outcome=outcome,
                now=now,
            )
            if updated.approval_status is ApprovalStatus.EXPIRED:
                uow.policy.save_action(updated)
                uow.commit()
                raise InvalidPolicyTransition("Approval has expired")
            assert action.approval_id is not None
            decision = ApprovalDecision.create(
                governed_action_id=action.id,
                approval_id=action.approval_id,
                approver_id=principal.principal_id,
                outcome=outcome,
                reason=reason,
                created_at=now,
            )
            uow.policy.save_action(updated)
            uow.policy.add_decision(decision)
            self._event(uow, updated, "agentmesh.policy.approval-decided")
            uow.commit()
            return GovernedActionResult(updated, (decision,))

    def consume_permit(
        self,
        permit_id: UUID | None,
        *,
        principal: PrincipalContext,
        action_type: GovernedActionType,
        resource_type: str,
        resource_id: UUID,
        arguments: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        self._require_principal(principal)
        if permit_id is None:
            raise ExecutionPermitRequired("A valid Execution-Permit-Id is required")
        normalized = self.normalize_arguments(action_type, arguments)
        expected_hash = canonical_action_hash(
            tenant_id=self._tenant_id,
            requester_id=principal.principal_id,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            arguments=normalized,
        )
        with self._uow_factory() as uow:
            action = uow.policy.get_by_permit(permit_id, for_update=True)
            if action is None or action.tenant_id != self._tenant_id:
                raise ExecutionPermitRequired("Execution Permit is invalid")
            if action.action_hash != expected_hash:
                raise ExecutionPermitRequired("Execution Permit does not match this action")
            updated = action.consume(now=datetime.now(timezone.utc))
            uow.policy.save_action(updated)
            self._event(uow, updated, "agentmesh.policy.permit-consumed")
            uow.commit()

    def list_approvals(
        self,
        *,
        status: ApprovalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GovernedActionResult]:
        with self._uow_factory() as uow:
            actions = uow.policy.list_actions(
                tenant_id=self._tenant_id,
                approval_status=status,
                limit=limit,
                offset=offset,
            )
            return [
                GovernedActionResult(action, tuple(uow.policy.list_decisions(action.id)))
                for action in actions
            ]

    @staticmethod
    def normalize_arguments(
        action_type: GovernedActionType,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(arguments)
        if action_type is GovernedActionType.AGENT_VERSION_PUBLISH:
            capabilities = normalized.get("verified_capabilities", [])
            if not isinstance(capabilities, list):
                raise InvalidPolicyTransition("verified_capabilities must be an array")
            normalized["verified_capabilities"] = sorted(set(str(value) for value in capabilities))
            normalized["make_default"] = bool(normalized.get("make_default", True))
        elif action_type is GovernedActionType.TASK_BUDGET_INCREASE:
            budget = normalized.get("budget")
            if not isinstance(budget, dict):
                raise InvalidPolicyTransition("budget must be an object")
            try:
                normalized["budget"] = TaskBudget.create(**budget).to_dict()
            except TypeError as exc:
                raise InvalidPolicyTransition("budget contains unknown fields") from exc
        return normalized

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise InvalidPolicyTransition(
                "Policy actions require an authenticated tenant Principal"
            )

    @staticmethod
    def _parse_rules(value: str) -> dict[GovernedActionType, PolicyResult]:
        try:
            raw = json.loads(value)
            if not isinstance(raw, dict):
                raise TypeError
            return {GovernedActionType(key): PolicyResult(result) for key, result in raw.items()}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidPolicyConfiguration(
                "policy_rules_json must map known actions to results"
            ) from exc

    @staticmethod
    def _event(uow: Any, action: GovernedAction, schema_name: str) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema_name,
                tenant_id=action.tenant_id,
                aggregate_id=action.id,
                payload={
                    "governed_action_id": str(action.id),
                    "action_type": action.action_type.value,
                    "policy_result": action.policy_result.value,
                    "approval_status": action.approval_status.value,
                },
            )
        )
