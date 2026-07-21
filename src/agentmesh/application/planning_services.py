from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    PlanPatchNotFound,
    TaskNotFound,
)
from agentmesh.domain.planning import GoalContract, PlanPatch, PlanPatchStatus
from agentmesh.domain.tasks import Task, TaskExecutionMode, TaskStatus
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class PlanningSnapshot:
    goal: GoalContract
    patches: tuple[PlanPatch, ...]


class PlanningApplicationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        max_concurrency: int,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._max_concurrency = max_concurrency
        self._feature_gates = feature_gates

    def get_snapshot(self, task_id: UUID) -> PlanningSnapshot:
        self._feature_gates.require(Feature.DYNAMIC_REPLANNING)
        with self._uow_factory() as uow:
            task = self._task(uow, task_id)
            goal = self._goal(uow, task)
            return PlanningSnapshot(goal, tuple(uow.plan_patches.list_for_task(task_id)))

    def propose_patch(
        self,
        task_id: UUID,
        *,
        base_plan_version: int,
        base_plan_digest: str,
        specs: tuple[SubtaskSpec, ...],
        max_concurrency: int,
        reason: str,
        requested_by: str,
    ) -> PlanPatch:
        self._feature_gates.require(Feature.DYNAMIC_REPLANNING)
        if max_concurrency > self._max_concurrency:
            raise InvalidTaskInput(
                "Plan Patch max_concurrency exceeds the platform limit of "
                f"{self._max_concurrency}"
            )
        with self._uow_factory() as uow:
            task = self._task(uow, task_id, for_update=True)
            self._require_replaceable(task)
            goal = self._goal(uow, task, for_update=True)
            current = self._current_plan(uow, task)
            proposed = CoordinatedPlan.create(
                specs,
                max_concurrency=max_concurrency,
                version=current.version + 1,
            )
            history_safe = not uow.runs.list_for_task(task.id) and not uow.handoffs.list_for_task(
                task.id
            )
            patch = PlanPatch.verify(
                task_id=task.id,
                goal=goal,
                current_plan=current,
                proposed_plan=proposed,
                base_plan_version=base_plan_version,
                base_plan_digest=base_plan_digest,
                reason=reason,
                requested_by=requested_by,
                history_safe=history_safe,
            )
            uow.plan_patches.add(patch)
            uow.commit()
            return patch

    def apply_patch(self, task_id: UUID, patch_id: UUID) -> PlanPatch:
        self._feature_gates.require(Feature.DYNAMIC_REPLANNING)
        with self._uow_factory() as uow:
            task = self._task(uow, task_id, for_update=True)
            patch = uow.plan_patches.get(patch_id, for_update=True)
            if patch is None or patch.task_id != task.id:
                raise PlanPatchNotFound(patch_id)
            if patch.status is PlanPatchStatus.APPLIED:
                return patch
            self._require_replaceable(task)
            goal = self._goal(uow, task, for_update=True)
            if (
                patch.goal_digest != goal.digest
                or patch.base_plan_version != task.plan_version
                or patch.base_plan_digest != task.plan_digest
            ):
                raise InvalidTaskTransition("Plan Patch base or Goal Contract is stale")
            if uow.runs.list_for_task(task.id) or uow.handoffs.list_for_task(task.id):
                raise InvalidTaskTransition("Plan Patch cannot rewrite execution history")
            proposed = patch.proposed_plan_snapshot()
            uow.subtask_dependencies.delete_for_task(task.id)
            uow.flush()
            uow.subtasks.delete_for_task(task.id)
            uow.flush()
            subtasks, dependencies = proposed.materialize(task.id)
            for subtask in subtasks:
                uow.subtasks.add(subtask)
            uow.flush()
            for dependency in dependencies:
                uow.subtask_dependencies.add(dependency)
            task.replace_plan(
                version=proposed.version,
                digest=proposed.digest,
                max_concurrency=proposed.max_concurrency,
            )
            patch.apply()
            uow.tasks.save(task)
            uow.plan_patches.save(patch)
            uow.commit()
            return patch

    def _task(self, uow: Any, task_id: UUID, *, for_update: bool = False) -> Task:
        task = uow.tasks.get(task_id, for_update=for_update)
        if task is None or task.tenant_id != self._tenant_id:
            raise TaskNotFound(task_id)
        return task

    @staticmethod
    def _goal(uow: Any, task: Task, *, for_update: bool = False) -> GoalContract:
        goal = uow.goal_contracts.get(task.id, for_update=for_update)
        if goal is None:
            raise InvalidTaskTransition("Task has no immutable Goal Contract")
        return goal

    @staticmethod
    def _require_replaceable(task: Task) -> None:
        if task.execution_mode is not TaskExecutionMode.COORDINATED:
            raise InvalidTaskTransition("Plan Patches require a coordinated Task")
        if task.status is not TaskStatus.CREATED:
            raise InvalidTaskTransition("The first Plan Patch slice is pre-execution only")

    @staticmethod
    def _current_plan(uow: Any, task: Task) -> CoordinatedPlan:
        if task.plan_version is None or task.plan_digest is None:
            raise InvalidTaskTransition("Coordinated Task has no current plan snapshot")
        subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        by_id = {subtask.id: subtask for subtask in subtasks}
        predecessors: dict[UUID, list[str]] = {subtask.id: [] for subtask in subtasks}
        for dependency in dependencies:
            predecessors[dependency.successor_id].append(by_id[dependency.predecessor_id].key)
        specs = tuple(
            SubtaskSpec.create(
                key=subtask.key,
                objective=subtask.objective,
                input=subtask.input,
                required_capabilities=subtask.required_capabilities,
                depends_on=tuple(sorted(predecessors[subtask.id])),
                preferred_agent_id=subtask.preferred_agent_id,
            )
            for subtask in subtasks
        )
        return CoordinatedPlan(
            version=task.plan_version,
            digest=task.plan_digest,
            max_concurrency=task.max_concurrency,
            specs=specs,
        )
