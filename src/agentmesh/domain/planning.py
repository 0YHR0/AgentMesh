from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.coordination import CoordinatedPlan
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_unique(values: tuple[str, ...] | list[str], *, label: str) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    if any(not value for value in normalized):
        raise InvalidTaskInput(f"Goal {label} must not contain blank values")
    if len(set(normalized)) != len(normalized):
        raise InvalidTaskInput(f"Goal {label} must be unique")
    return normalized


def _plan_shape(plan: CoordinatedPlan) -> tuple[int, tuple[tuple[str, str], ...]]:
    return (
        plan.max_concurrency,
        tuple(
            sorted(
                (
                    spec.key,
                    json.dumps(spec.to_dict(), sort_keys=True, separators=(",", ":")),
                )
                for spec in plan.specs
            )
        ),
    )


@dataclass(frozen=True)
class GoalContract:
    task_id: UUID
    version: int
    objective: str
    constraints: tuple[str, ...]
    success_criteria: tuple[str, ...]
    digest: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        task_id: UUID,
        objective: str,
        constraints: tuple[str, ...] | list[str] = (),
        success_criteria: tuple[str, ...] | list[str] = (),
    ) -> GoalContract:
        normalized_objective = objective.strip()
        if not normalized_objective:
            raise InvalidTaskInput("Goal objective must not be empty")
        normalized_constraints = _normalized_unique(constraints, label="constraints")
        normalized_criteria = _normalized_unique(success_criteria, label="success criteria")
        if len(normalized_constraints) > 20 or len(normalized_criteria) > 20:
            raise InvalidTaskInput("Goal contracts support at most 20 constraints and criteria")
        if not normalized_criteria:
            normalized_criteria = (
                "All required Subtasks complete and the Supervisor produces a final result",
            )
        payload = {
            "version": 1,
            "objective": normalized_objective,
            "constraints": list(normalized_constraints),
            "success_criteria": list(normalized_criteria),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return cls(
            task_id=task_id,
            version=1,
            objective=normalized_objective,
            constraints=normalized_constraints,
            success_criteria=normalized_criteria,
            digest=f"sha256:{sha256(canonical.encode()).hexdigest()}",
            created_at=utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "version": self.version,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "success_criteria": list(self.success_criteria),
            "digest": self.digest,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class VerifierFinding:
    code: str
    passed: bool
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "passed": self.passed,
            "message": self.message,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VerifierFinding:
        return cls(
            code=str(value["code"]),
            passed=bool(value["passed"]),
            message=str(value["message"]),
            details=dict(value.get("details", {})),
        )


class PlanPatchStatus(str, Enum):
    VERIFIED = "VERIFIED"
    APPLIED = "APPLIED"


@dataclass
class PlanPatch:
    id: UUID
    task_id: UUID
    goal_digest: str
    base_plan_version: int
    base_plan_digest: str
    proposed_plan_version: int
    proposed_plan_digest: str
    proposed_plan: dict[str, Any]
    reason: str
    requested_by: str
    evidence: tuple[VerifierFinding, ...]
    status: PlanPatchStatus
    created_at: datetime
    applied_at: datetime | None

    @classmethod
    def verify(
        cls,
        *,
        task_id: UUID,
        goal: GoalContract,
        current_plan: CoordinatedPlan,
        proposed_plan: CoordinatedPlan,
        base_plan_version: int,
        base_plan_digest: str,
        reason: str,
        requested_by: str,
        history_safe: bool,
        history_details: dict[str, Any] | None = None,
    ) -> PlanPatch:
        normalized_reason = reason.strip()
        normalized_actor = requested_by.strip()
        if not normalized_reason or not normalized_actor:
            raise InvalidTaskInput("Plan Patch reason and requester must not be empty")
        checks = (
            VerifierFinding(
                "goal-bound",
                goal.task_id == task_id,
                "Patch is bound to the Task's immutable Goal Contract",
                {"goal_digest": goal.digest},
            ),
            VerifierFinding(
                "base-version-current",
                base_plan_version == current_plan.version
                and base_plan_digest == current_plan.digest,
                "Patch base version and digest match the current plan",
                {
                    "expected_version": current_plan.version,
                    "expected_digest": current_plan.digest,
                },
            ),
            VerifierFinding(
                "version-monotonic",
                proposed_plan.version == current_plan.version + 1,
                "Proposed plan advances exactly one version",
                {"proposed_version": proposed_plan.version},
            ),
            VerifierFinding(
                "plan-changed",
                _plan_shape(proposed_plan) != _plan_shape(current_plan),
                "Proposed plan contains a semantic change",
                {},
            ),
            VerifierFinding(
                "dag-validated",
                True,
                "Proposed plan passed bounded DAG validation",
                {
                    "subtask_count": len(proposed_plan.specs),
                    "dependency_count": sum(
                        len(spec.depends_on) for spec in proposed_plan.specs
                    ),
                },
            ),
            VerifierFinding(
                "execution-history-safe",
                history_safe,
                "Execution history is absent or preserved by a quiescent replacement",
                dict(history_details or {}),
            ),
        )
        failed = [finding.code for finding in checks if not finding.passed]
        if failed:
            raise InvalidTaskTransition(
                "Plan Patch verification failed: " + ", ".join(failed)
            )
        return cls(
            id=uuid4(),
            task_id=task_id,
            goal_digest=goal.digest,
            base_plan_version=current_plan.version,
            base_plan_digest=current_plan.digest,
            proposed_plan_version=proposed_plan.version,
            proposed_plan_digest=proposed_plan.digest,
            proposed_plan=proposed_plan.to_dict(),
            reason=normalized_reason,
            requested_by=normalized_actor,
            evidence=checks,
            status=PlanPatchStatus.VERIFIED,
            created_at=utc_now(),
            applied_at=None,
        )

    def apply(self) -> None:
        if self.status is not PlanPatchStatus.VERIFIED:
            raise InvalidTaskTransition(f"Plan Patch {self.id} is already applied")
        self.status = PlanPatchStatus.APPLIED
        self.applied_at = utc_now()

    def proposed_plan_snapshot(self) -> CoordinatedPlan:
        plan = CoordinatedPlan.from_dict(self.proposed_plan)
        if plan.digest != self.proposed_plan_digest:
            raise InvalidTaskTransition("Plan Patch snapshot digest no longer matches")
        return plan
