from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.coordination import (
    CoordinatedPlan,
    Subtask,
    SubtaskDependency,
    SubtaskSpec,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    PlanPatchNotFound,
    TaskNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.planning import GoalContract, PlanPatch, PlanPatchStatus
from agentmesh.domain.tasks import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    Task,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.domain.tools import ToolInvocationStatus, ToolSideEffect
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
                f"Plan Patch max_concurrency exceeds the platform limit of {self._max_concurrency}"
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
            history_details = self._replacement_guard(uow, task, current, proposed)
            patch = PlanPatch.verify(
                task_id=task.id,
                goal=goal,
                current_plan=current,
                proposed_plan=proposed,
                base_plan_version=base_plan_version,
                base_plan_digest=base_plan_digest,
                reason=reason,
                requested_by=requested_by,
                history_safe=True,
                history_details=history_details,
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
            proposed = patch.proposed_plan_snapshot()
            current = self._current_plan(uow, task)
            self._replacement_guard(uow, task, current, proposed)
            if task.status is TaskStatus.CREATED:
                self._replace_pre_execution(uow, task, proposed)
            else:
                self._replace_quiescent(uow, task, proposed)
            task.replace_plan(
                version=proposed.version,
                digest=proposed.digest,
                max_concurrency=proposed.max_concurrency,
            )
            patch.apply()
            uow.tasks.save(task)
            uow.plan_patches.save(patch)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.task.plan-patch-applied",
                    tenant_id=task.tenant_id,
                    aggregate_id=task.id,
                    payload={
                        "task_id": str(task.id),
                        "patch_id": str(patch.id),
                        "plan_version": proposed.version,
                        "plan_digest": proposed.digest,
                    },
                )
            )
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
        if task.status not in {TaskStatus.CREATED, TaskStatus.WAITING_APPROVAL}:
            raise InvalidTaskTransition(
                "Plan Patches require a CREATED or quiescent WAITING_APPROVAL Task"
            )
        if task.status is TaskStatus.WAITING_APPROVAL and task.budget_exhausted_reason is None:
            raise InvalidTaskTransition(
                "Running Plan Patches require an explicit budget waiting barrier"
            )

    @staticmethod
    def _replace_pre_execution(uow: Any, task: Task, proposed: CoordinatedPlan) -> None:
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

    @staticmethod
    def _replace_quiescent(uow: Any, task: Task, proposed: CoordinatedPlan) -> None:
        current_subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        preserved = {
            subtask.key: subtask
            for subtask in current_subtasks
            if subtask.status is SubtaskStatus.COMPLETED
        }
        removable = [subtask.id for subtask in current_subtasks if subtask.key not in preserved]
        uow.subtask_dependencies.delete_for_task(task.id)
        uow.flush()
        uow.subtasks.delete_ids(task.id, removable)
        uow.flush()

        id_by_key = {
            spec.key: preserved[spec.key].id if spec.key in preserved else uuid4()
            for spec in proposed.specs
        }
        completed_keys = set(preserved)
        for spec in proposed.specs:
            if spec.key in preserved:
                continue
            uow.subtasks.add(
                Subtask.create(
                    subtask_id=id_by_key[spec.key],
                    task_id=task.id,
                    key=spec.key,
                    objective=spec.objective,
                    input=spec.input,
                    required_capabilities=spec.required_capabilities,
                    preferred_agent_id=spec.preferred_agent_id,
                    initially_ready=set(spec.depends_on).issubset(completed_keys),
                )
            )
        uow.flush()
        for spec in proposed.specs:
            for predecessor in spec.depends_on:
                uow.subtask_dependencies.add(
                    SubtaskDependency(
                        task_id=task.id,
                        predecessor_id=id_by_key[predecessor],
                        successor_id=id_by_key[spec.key],
                    )
                )

    @staticmethod
    def _replacement_guard(
        uow: Any,
        task: Task,
        current: CoordinatedPlan,
        proposed: CoordinatedPlan,
    ) -> dict[str, Any]:
        if task.status is TaskStatus.CREATED:
            if uow.runs.list_for_task(task.id) or uow.handoffs.list_for_task(task.id):
                raise InvalidTaskTransition("Plan Patch cannot rewrite execution history")
            return {"mode": "pre-execution", "preserved_subtasks": 0}

        runs = uow.runs.list_for_task(task.id)
        if any(run.status not in TERMINAL_RUN_STATUSES for run in runs):
            raise InvalidTaskTransition("Running Plan Patch requires all Runs to be terminal")
        attempts = uow.attempts.list_for_task(task.id)
        if any(
            attempt.status in {AttemptStatus.RUNNING, AttemptStatus.PAUSED} for attempt in attempts
        ):
            raise InvalidTaskTransition("Running Plan Patch requires all Attempts to be terminal")
        if uow.handoffs.list_for_task(task.id):
            raise InvalidTaskTransition("Running Plan Patch cannot supersede Handoff history")
        if uow.remote_correlations.get_for_task(task.id) is not None:
            raise InvalidTaskTransition("Running Plan Patch cannot supersede A2A delegation")
        invocations = uow.tool_invocations.list_for_task(task.id)
        if any(
            invocation.side_effect is not ToolSideEffect.READ_ONLY
            or invocation.status
            in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.OUTCOME_UNKNOWN}
            for invocation in invocations
        ):
            raise InvalidTaskTransition(
                "Running Plan Patch is blocked by active or write-class Tool history"
            )

        subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        current_by_key = {spec.key: spec for spec in current.specs}
        proposed_by_key = {spec.key: spec for spec in proposed.specs}
        completed = [subtask for subtask in subtasks if subtask.status is SubtaskStatus.COMPLETED]
        historical = [subtask for subtask in subtasks if subtask.current_run_id is not None]
        if any(subtask.status is not SubtaskStatus.COMPLETED for subtask in historical):
            raise InvalidTaskTransition(
                "Running Plan Patch cannot supersede non-completed Subtask history"
            )
        for subtask in completed:
            if proposed_by_key.get(subtask.key) != current_by_key[subtask.key]:
                raise InvalidTaskTransition(
                    f"Running Plan Patch must preserve completed Subtask {subtask.key}"
                )

        completed_keys = {subtask.key for subtask in completed}
        current_remaining = len(current.specs) - len(completed_keys)
        proposed_remaining = len(proposed.specs) - len(completed_keys)
        if proposed_remaining > current_remaining:
            raise InvalidTaskTransition(
                "Running Plan Patch cannot increase remaining Subtask count"
            )
        if proposed.max_concurrency > current.max_concurrency:
            raise InvalidTaskTransition("Running Plan Patch cannot increase max concurrency")
        return {
            "mode": "quiescent-running",
            "terminal_runs": len(runs),
            "preserved_subtasks": len(completed),
            "remaining_before": current_remaining,
            "remaining_after": proposed_remaining,
            "max_concurrency_non_increasing": True,
            "external_side_effects": False,
        }

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
