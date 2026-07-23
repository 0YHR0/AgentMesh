from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.identity import Role
from agentmesh.domain.policy import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalStage,
    ApprovalStatus,
    GovernedAction,
    GovernedActionType,
    PolicyResult,
)
from agentmesh.infrastructure.postgres.models import ApprovalDecisionRecord, GovernedActionRecord


class SqlAlchemyPolicyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_action(self, action: GovernedAction) -> None:
        self._session.add(self._to_record(action))

    def get_action(self, action_id: UUID, *, for_update: bool = False) -> GovernedAction | None:
        return self._one(
            select(GovernedActionRecord).where(GovernedActionRecord.id == action_id), for_update
        )

    def get_by_approval(
        self, approval_id: UUID, *, for_update: bool = False
    ) -> GovernedAction | None:
        return self._one(
            select(GovernedActionRecord).where(GovernedActionRecord.approval_id == approval_id),
            for_update,
        )

    def get_by_permit(self, permit_id: UUID, *, for_update: bool = False) -> GovernedAction | None:
        return self._one(
            select(GovernedActionRecord).where(GovernedActionRecord.permit_id == permit_id),
            for_update,
        )

    def _one(self, statement, for_update: bool) -> GovernedAction | None:
        if for_update:
            statement = statement.with_for_update()
        record = self._session.execute(statement).scalar_one_or_none()
        return self._to_domain(record) if record is not None else None

    def save_action(self, action: GovernedAction) -> None:
        record = self._session.get(GovernedActionRecord, action.id)
        if record is None:
            raise LookupError(action.id)
        record.approval_status = action.approval_status.value
        record.permit_id = action.permit_id
        record.current_stage = action.current_stage
        record.decided_at = action.decided_at
        record.consumed_at = action.consumed_at
        record.revision = action.revision

    def list_actions(
        self, *, tenant_id: str, approval_status: ApprovalStatus | None, limit: int, offset: int
    ) -> list[GovernedAction]:
        statement = select(GovernedActionRecord).where(
            GovernedActionRecord.tenant_id == tenant_id,
            GovernedActionRecord.approval_id.is_not(None),
        )
        if approval_status is not None:
            statement = statement.where(
                GovernedActionRecord.approval_status == approval_status.value
            )
        records = self._session.execute(
            statement.order_by(GovernedActionRecord.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
        return [self._to_domain(record) for record in records]

    def list_actions_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: UUID
    ) -> list[GovernedAction]:
        records = self._session.execute(
            select(GovernedActionRecord)
            .where(
                GovernedActionRecord.tenant_id == tenant_id,
                GovernedActionRecord.resource_type == resource_type,
                GovernedActionRecord.resource_id == resource_id,
            )
            .order_by(GovernedActionRecord.created_at)
        ).scalars()
        return [self._to_domain(record) for record in records]

    def add_decision(self, decision: ApprovalDecision) -> None:
        self._session.add(
            ApprovalDecisionRecord(
                id=decision.id,
                governed_action_id=decision.governed_action_id,
                approval_id=decision.approval_id,
                approver_id=decision.approver_id,
                outcome=decision.outcome.value,
                stage=decision.stage,
                reason=decision.reason,
                created_at=decision.created_at,
            )
        )

    def list_decisions(self, governed_action_id: UUID) -> list[ApprovalDecision]:
        records = self._session.execute(
            select(ApprovalDecisionRecord)
            .where(ApprovalDecisionRecord.governed_action_id == governed_action_id)
            .order_by(ApprovalDecisionRecord.created_at)
        ).scalars()
        return [
            ApprovalDecision(
                id=record.id,
                governed_action_id=record.governed_action_id,
                approval_id=record.approval_id,
                approver_id=record.approver_id,
                outcome=ApprovalOutcome(record.outcome),
                stage=record.stage,
                reason=record.reason,
                created_at=record.created_at,
            )
            for record in records
        ]

    @staticmethod
    def _to_record(action: GovernedAction) -> GovernedActionRecord:
        return GovernedActionRecord(
            id=action.id,
            tenant_id=action.tenant_id,
            requester_id=action.requester_id,
            action_type=action.action_type.value,
            resource_type=action.resource_type,
            resource_id=action.resource_id,
            arguments=action.arguments,
            canonicalization_version=action.canonicalization_version,
            action_hash=action.action_hash,
            policy_result=action.policy_result.value,
            reason_code=action.reason_code,
            policy_bundle=action.policy_bundle,
            policy_version=action.policy_version,
            obligations=action.obligations,
            approval_stages=[
                {
                    "name": stage.name,
                    "quorum": stage.quorum,
                    "eligible_roles": [role.value for role in stage.eligible_roles],
                }
                for stage in action.approval_stages
            ],
            current_stage=action.current_stage,
            approval_id=action.approval_id,
            approval_status=action.approval_status.value,
            permit_id=action.permit_id,
            created_at=action.created_at,
            expires_at=action.expires_at,
            decided_at=action.decided_at,
            consumed_at=action.consumed_at,
            revision=action.revision,
        )

    @staticmethod
    def _to_domain(record: GovernedActionRecord) -> GovernedAction:
        return GovernedAction(
            id=record.id,
            tenant_id=record.tenant_id,
            requester_id=record.requester_id,
            action_type=GovernedActionType(record.action_type),
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            arguments=dict(record.arguments),
            canonicalization_version=record.canonicalization_version,
            action_hash=record.action_hash,
            policy_result=PolicyResult(record.policy_result),
            reason_code=record.reason_code,
            policy_bundle=record.policy_bundle,
            policy_version=record.policy_version,
            obligations=dict(record.obligations),
            approval_stages=tuple(
                ApprovalStage(
                    name=stage["name"],
                    quorum=int(stage["quorum"]),
                    eligible_roles=tuple(Role(role) for role in stage["eligible_roles"]),
                )
                for stage in record.approval_stages
            ),
            current_stage=record.current_stage,
            approval_id=record.approval_id,
            approval_status=ApprovalStatus(record.approval_status),
            permit_id=record.permit_id,
            created_at=record.created_at,
            expires_at=record.expires_at,
            decided_at=record.decided_at,
            consumed_at=record.consumed_at,
            revision=record.revision,
        )
