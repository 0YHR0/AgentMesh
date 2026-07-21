from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.planning import (
    GoalContract,
    PlanPatch,
    PlanPatchStatus,
    VerifierFinding,
)
from agentmesh.infrastructure.postgres.models import GoalContractRecord, PlanPatchRecord


class SqlAlchemyGoalContractRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, goal: GoalContract) -> None:
        self._session.add(
            GoalContractRecord(
                task_id=goal.task_id,
                version=goal.version,
                objective=goal.objective,
                constraints=list(goal.constraints),
                success_criteria=list(goal.success_criteria),
                digest=goal.digest,
                created_at=goal.created_at,
            )
        )

    def get(self, task_id: UUID, *, for_update: bool = False) -> GoalContract | None:
        record = self._session.get(GoalContractRecord, task_id, with_for_update=for_update)
        if record is None:
            return None
        return GoalContract(
            task_id=record.task_id,
            version=record.version,
            objective=record.objective,
            constraints=tuple(record.constraints),
            success_criteria=tuple(record.success_criteria),
            digest=record.digest,
            created_at=record.created_at,
        )


class SqlAlchemyPlanPatchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, patch: PlanPatch) -> None:
        self._session.add(self._to_record(patch))

    def get(self, patch_id: UUID, *, for_update: bool = False) -> PlanPatch | None:
        record = self._session.get(PlanPatchRecord, patch_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, patch: PlanPatch) -> None:
        record = self._session.get(PlanPatchRecord, patch.id)
        if record is None:
            raise LookupError(patch.id)
        record.status = patch.status.value
        record.applied_at = patch.applied_at

    def list_for_task(self, task_id: UUID) -> list[PlanPatch]:
        statement = (
            select(PlanPatchRecord)
            .where(PlanPatchRecord.task_id == task_id)
            .order_by(PlanPatchRecord.created_at.asc(), PlanPatchRecord.id.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(patch: PlanPatch) -> PlanPatchRecord:
        return PlanPatchRecord(
            id=patch.id,
            task_id=patch.task_id,
            goal_digest=patch.goal_digest,
            base_plan_version=patch.base_plan_version,
            base_plan_digest=patch.base_plan_digest,
            proposed_plan_version=patch.proposed_plan_version,
            proposed_plan_digest=patch.proposed_plan_digest,
            proposed_plan=dict(patch.proposed_plan),
            reason=patch.reason,
            requested_by=patch.requested_by,
            evidence=[finding.to_dict() for finding in patch.evidence],
            status=patch.status.value,
            created_at=patch.created_at,
            applied_at=patch.applied_at,
        )

    @staticmethod
    def _to_domain(record: PlanPatchRecord) -> PlanPatch:
        return PlanPatch(
            id=record.id,
            task_id=record.task_id,
            goal_digest=record.goal_digest,
            base_plan_version=record.base_plan_version,
            base_plan_digest=record.base_plan_digest,
            proposed_plan_version=record.proposed_plan_version,
            proposed_plan_digest=record.proposed_plan_digest,
            proposed_plan=dict(record.proposed_plan),
            reason=record.reason,
            requested_by=record.requested_by,
            evidence=tuple(VerifierFinding.from_dict(value) for value in record.evidence),
            status=PlanPatchStatus(record.status),
            created_at=record.created_at,
            applied_at=record.applied_at,
        )
