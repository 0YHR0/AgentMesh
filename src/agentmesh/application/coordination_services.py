from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.agent_resolution import (
    resolve_capable_agent as resolve_agent_by_name,
)
from agentmesh.application.agent_resolution import (
    version_satisfies as agent_version_satisfies,
)
from agentmesh.application.authority_cohorts import (
    AuthorityCohort,
    AuthorityCohortResolver,
    ContinuationKind,
)
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.errors import AgentUnavailable, InvalidTaskTransition
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentVersion,
    AgentVersionStatus,
)
from agentmesh.domain.tasks import RunRole, RunStatus, Task, TaskRun, TaskStatus, utc_now
from agentmesh.features import FeatureGateSet


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return (
            "__dict__",
            tuple(sorted((str(key), _freeze_json(item)) for key, item in value.items())),
        )
    if isinstance(value, (list, tuple)):
        return ("__list__", tuple(_freeze_json(item) for item in value))
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2:
        if value[0] == "__dict__":
            return {item[0]: _thaw_json(item[1]) for item in value[1]}
        if value[0] == "__list__":
            return [_thaw_json(item) for item in value[1]]
    return value


@dataclass(frozen=True)
class PlannedRunSpec:
    """Immutable detached representation of a queued coordinated Run."""

    id: UUID
    task_id: UUID
    thread_id: str
    agent_id: str
    agent_version_id: UUID | None
    agent_version_digest: str | None
    role: RunRole
    revision_number: int
    subtask_id: UUID | None
    status: RunStatus
    queued_at: datetime
    runtime_version_id: UUID | None
    runtime_execution_id: UUID | None
    runtime_execution_intent_id: UUID | None
    runtime_authority: str
    comparison_mode: str

    @classmethod
    def from_run(cls, run: TaskRun) -> PlannedRunSpec:
        return cls(
            id=run.id,
            task_id=run.task_id,
            thread_id=run.thread_id,
            agent_id=run.agent_id,
            agent_version_id=run.agent_version_id,
            agent_version_digest=run.agent_version_digest,
            role=run.role,
            revision_number=run.revision_number,
            subtask_id=run.subtask_id,
            status=run.status,
            queued_at=run.queued_at,
            runtime_version_id=run.runtime_version_id,
            runtime_execution_id=run.runtime_execution_id,
            runtime_execution_intent_id=run.runtime_execution_intent_id,
            runtime_authority=run.runtime_authority,
            comparison_mode=run.comparison_mode,
        )

    def materialize(self) -> TaskRun:
        return TaskRun(
            id=self.id,
            task_id=self.task_id,
            thread_id=self.thread_id,
            agent_id=self.agent_id,
            agent_version_id=self.agent_version_id,
            agent_version_digest=self.agent_version_digest,
            role=self.role,
            revision_number=self.revision_number,
            subtask_id=self.subtask_id,
            status=self.status,
            output=None,
            error=None,
            queued_at=self.queued_at,
            started_at=None,
            completed_at=None,
            pause_requested_at=None,
            paused_at=None,
            resumed_at=None,
            paused_from_status=None,
            runtime_version_id=self.runtime_version_id,
            runtime_execution_id=self.runtime_execution_id,
            runtime_execution_intent_id=self.runtime_execution_intent_id,
            runtime_authority=self.runtime_authority,
            comparison_mode=self.comparison_mode,
        )


@dataclass(frozen=True)
class CoordinatedSchedulePlan:
    """Read-only coordination decision applied in a later transaction phase.

    The snapshots form a compare-and-swap token.  ``planned_runs`` are detached
    domain values and are not visible to a repository until :meth:`apply`.
    """

    task_id: UUID
    task_version: int
    task_status: TaskStatus
    task_current_run_id: UUID | None
    plan_version: int | None
    plan_digest: str | None
    subtask_snapshot: tuple[tuple[UUID, int, SubtaskStatus, UUID | None, Any], ...]
    run_snapshot: tuple[tuple[Any, ...], ...]
    ready_subtask_ids: tuple[UUID, ...]
    planned_runs: tuple[PlannedRunSpec, ...]
    cohort: AuthorityCohort
    at: datetime
    causation_id: UUID | None
    hypothetical_subtask_id: UUID | None = None
    hypothetical_output: Any = None
    budget_rejection: str | None = None
    wait_for_budget: bool = False
    # A second immutable copy makes the plan's detached Run specs tamper
    # evident: ``dataclasses.replace(plan, planned_runs=...)`` retains this
    # token and is rejected by apply before any business mutation.
    planned_run_snapshot: tuple[PlannedRunSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.planned_run_snapshot:
            object.__setattr__(self, "planned_run_snapshot", tuple(self.planned_runs))
        if self.hypothetical_output is not None:
            object.__setattr__(self, "hypothetical_output", _freeze_json(self.hypothetical_output))


class CoordinatedScheduler:
    """Deterministic, transaction-local scheduler for the bounded DAG slice."""

    def __init__(
        self,
        *,
        supervisor_agent_id: str,
        authority_cohort_resolver: AuthorityCohortResolver | None = None,
    ) -> None:
        self._supervisor_agent_id = supervisor_agent_id
        self._authority_cohort_resolver = authority_cohort_resolver or AuthorityCohortResolver(
            feature_gates=FeatureGateSet.from_config("minimal")
        )

    def start(
        self,
        uow: Any,
        task: Task,
        *,
        at: datetime | None = None,
        causation_id: UUID | None = None,
    ) -> list[TaskRun]:
        accepted_by_target = self._accepted_by_target(uow, task.id)
        for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
            self._resolve_subtask_agent(
                uow, task.tenant_id, subtask, accepted_by_target.get(subtask.id)
            )
        task.start_coordination(at=at)
        # Persist the start transition before entering the read-only planner;
        # the caller-owned UoW still rolls this back if planning fails.
        uow.tasks.save(task)
        return self.schedule(uow, task, at=at, causation_id=causation_id)

    def schedule(
        self,
        uow: Any,
        task: Task,
        *,
        at: datetime | None = None,
        causation_id: UUID | None = None,
    ) -> list[TaskRun]:
        if task.status != TaskStatus.RUNNING:
            return []
        # Legacy callers may have performed accounting on the aggregate in
        # this UoW immediately before asking the scheduler to continue.  The
        # compatibility wrapper owns that synchronization; the read-only
        # ``plan`` method itself never writes.
        persisted = uow.tasks.get(task.id)
        if persisted is not None and (
            persisted.version != task.version or persisted.status != task.status
        ):
            uow.tasks.save(task)
        plan = self.plan(uow, task, at=at, causation_id=causation_id)
        created = list(self.apply(uow, plan))
        # Keep the compatibility caller's aggregate in sync with the detached
        # repository value used by the CAS apply phase.
        persisted = uow.tasks.get(task.id)
        if persisted is not None:
            task.__dict__.update(deepcopy(persisted.__dict__))
        return created

    def plan(
        self,
        uow: Any,
        task: Task,
        *,
        at: datetime | None = None,
        causation_id: UUID | None = None,
        completing_subtask_id: UUID | None = None,
        completed_subtask_id: UUID | None = None,
        completion_output: dict[str, Any] | None = None,
        target_subtask_id: UUID | None = None,
        target_output: dict[str, Any] | None = None,
    ) -> CoordinatedSchedulePlan:
        """Build a coordination decision without business writes.

        ``completed_subtask_id`` is a hypothetical completion used by the
        outcome applier.  ``target_*`` aliases are accepted for callers that
        describe the same barrier target using scheduler terminology.
        """
        if completing_subtask_id is not None:
            if completed_subtask_id is not None and completing_subtask_id != completed_subtask_id:
                raise InvalidTaskTransition("Conflicting coordinated plan targets")
            completed_subtask_id = completing_subtask_id
        if target_subtask_id is not None:
            if completed_subtask_id is not None and target_subtask_id != completed_subtask_id:
                raise InvalidTaskTransition("Conflicting coordinated plan targets")
            completed_subtask_id = target_subtask_id
        if target_output is not None:
            if completion_output is not None and target_output != completion_output:
                raise InvalidTaskTransition("Conflicting coordinated completion outputs")
            completion_output = target_output
        if task.status != TaskStatus.RUNNING:
            raise InvalidTaskTransition("Coordinated scheduling requires a running Task")
        policy_at = self._normalize_at(at)
        if causation_id is not None and type(causation_id) is not UUID:
            raise InvalidTaskTransition("Coordinated plan causation ID is invalid")
        persisted_task = uow.tasks.get(task.id, for_update=True)
        if persisted_task is None or persisted_task.tenant_id != task.tenant_id:
            raise InvalidTaskTransition("Coordinated plan Task is unavailable")
        if persisted_task.version != task.version or persisted_task.status != task.status:
            raise InvalidTaskTransition("Coordinated plan Task changed before planning")
        subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        accepted_by_target = self._accepted_by_target(uow, task.id)
        cohort = self._authority_cohort_resolver.resolve_continuation_cohort_in_uow(
            uow, persisted_task
        )
        by_id = {subtask.id: subtask for subtask in subtasks}
        virtual = {subtask.id: deepcopy(subtask) for subtask in subtasks}
        if completed_subtask_id is not None:
            target = virtual.get(completed_subtask_id)
            if target is None or target.status is not SubtaskStatus.RUNNING:
                raise InvalidTaskTransition("Coordinated completion target is not running")
            if target.current_run_id is None or completion_output is None:
                raise InvalidTaskTransition("Coordinated completion target is incomplete")
            target.complete(target.current_run_id, completion_output, at=policy_at)
        predecessors: dict[UUID, set[UUID]] = {subtask.id: set() for subtask in subtasks}
        for dependency in dependencies:
            if dependency.successor_id not in by_id or dependency.predecessor_id not in by_id:
                raise InvalidTaskTransition("Coordinated dependency references an unknown Subtask")
            predecessors[dependency.successor_id].add(dependency.predecessor_id)
        ready_ids: list[UUID] = []
        for subtask in sorted(virtual.values(), key=lambda value: value.key):
            if subtask.status is SubtaskStatus.BLOCKED and all(
                virtual[predecessor_id].status is SubtaskStatus.COMPLETED
                for predecessor_id in predecessors[subtask.id]
            ):
                subtask.mark_ready(at=policy_at)
                ready_ids.append(subtask.id)

        existing_runs = uow.runs.list_for_task(task.id)
        run_snapshot = tuple(self._run_fingerprint(run) for run in existing_runs)
        planned: list[TaskRun] = []
        rejection: str | None = None
        wait_for_budget = False
        if virtual and all(
            subtask.status is SubtaskStatus.COMPLETED for subtask in virtual.values()
        ):
            if persisted_task.current_run_id is None:
                rejection = self._plan_run_rejection(
                    uow, persisted_task, existing_runs, len(planned), now=policy_at
                )
                if rejection is not None:
                    wait_for_budget = True
                else:
                    agent_name, agent_version = self.resolve_named_agent(
                        uow,
                        persisted_task.tenant_id,
                        self._supervisor_agent_id,
                        {"general.supervise"},
                    )
                    planned.append(
                        self._new_run(
                            uow,
                            persisted_task,
                            agent_name,
                            agent_version_id=agent_version.id,
                            agent_version_digest=agent_version.content_digest,
                            role=RunRole.SUPERVISOR,
                            cohort=cohort,
                            kind=ContinuationKind.COORDINATED,
                            at=policy_at,
                        )
                    )
        else:
            active = sum(
                1
                for subtask in virtual.values()
                if subtask.current_run_id is not None
                and subtask.status in {SubtaskStatus.READY, SubtaskStatus.RUNNING}
            )
            available = max(persisted_task.max_concurrency - active, 0)
            for subtask in sorted(virtual.values(), key=lambda value: value.key):
                if available == 0 or subtask.status is not SubtaskStatus.READY:
                    continue
                if subtask.current_run_id is not None:
                    continue
                rejection = self._plan_run_rejection(
                    uow, persisted_task, existing_runs, len(planned), now=policy_at
                )
                if rejection is not None:
                    wait_for_budget = active == 0 and not planned
                    break
                agent_name, agent_version = self._resolve_subtask_agent(
                    uow, persisted_task.tenant_id, subtask, accepted_by_target.get(subtask.id)
                )
                run = self._new_run(
                    uow,
                    persisted_task,
                    agent_name,
                    agent_version_id=agent_version.id,
                    agent_version_digest=agent_version.content_digest,
                    role=RunRole.EXECUTOR,
                    subtask_id=subtask.id,
                    cohort=cohort,
                    kind=ContinuationKind.COORDINATED,
                    at=policy_at,
                )
                subtask.queue(run.id, at=policy_at)
                planned.append(run)
                available -= 1
        snapshots = tuple(
            (
                subtask.id,
                subtask.version,
                subtask.status,
                subtask.current_run_id,
                _freeze_json(subtask.output),
            )
            for subtask in subtasks
        )
        return CoordinatedSchedulePlan(
            task_id=persisted_task.id,
            task_version=persisted_task.version,
            task_status=persisted_task.status,
            task_current_run_id=persisted_task.current_run_id,
            plan_version=persisted_task.plan_version,
            plan_digest=persisted_task.plan_digest,
            subtask_snapshot=snapshots,
            run_snapshot=run_snapshot,
            ready_subtask_ids=tuple(ready_ids),
            planned_runs=tuple(PlannedRunSpec.from_run(run) for run in planned),
            cohort=cohort,
            at=policy_at,
            causation_id=causation_id,
            hypothetical_subtask_id=completed_subtask_id,
            hypothetical_output=completion_output,
            budget_rejection=rejection,
            wait_for_budget=wait_for_budget,
            planned_run_snapshot=tuple(PlannedRunSpec.from_run(run) for run in planned),
        )

    def apply(
        self,
        uow: Any,
        task_or_plan: Task | CoordinatedSchedulePlan,
        plan: CoordinatedSchedulePlan | None = None,
    ) -> tuple[TaskRun, ...]:
        """Compare-and-swap a plan, then persist its state transitions."""
        if plan is None:
            if not isinstance(task_or_plan, CoordinatedSchedulePlan):
                raise InvalidTaskTransition("Coordinated schedule plan is required")
            plan = task_or_plan
        elif not isinstance(plan, CoordinatedSchedulePlan):
            raise InvalidTaskTransition("Coordinated schedule plan is invalid")
        elif not isinstance(task_or_plan, Task) or task_or_plan.id != plan.task_id:
            raise InvalidTaskTransition("Coordinated schedule Task does not match plan")
        task = uow.tasks.get(plan.task_id, for_update=True)
        if task is None or not self._task_matches_plan(task, plan):
            raise InvalidTaskTransition("Coordinated schedule plan is stale")
        subtasks = uow.subtasks.list_for_task(plan.task_id, for_update=True)
        runs = uow.runs.list_for_task(plan.task_id)
        self._validate_apply_plan(uow, task, plan, subtasks, runs)
        if not self._runs_match_plan(runs, plan):
            raise InvalidTaskTransition("Coordinated schedule Run state is stale")
        by_id = {subtask.id: subtask for subtask in subtasks}
        for subtask_id in plan.ready_subtask_ids:
            subtask = by_id[subtask_id]
            if subtask.status is SubtaskStatus.BLOCKED:
                subtask.mark_ready(at=plan.at)
                uow.subtasks.save(subtask)
        if plan.wait_for_budget and plan.budget_rejection is not None:
            task.wait_for_budget(plan.budget_rejection, at=plan.at)
            uow.tasks.save(task)
            return ()
        persisted_runs: list[TaskRun] = []
        for planned_spec in plan.planned_runs:
            run = planned_spec.materialize()
            if run.role is RunRole.SUPERVISOR:
                task.queue_supervisor(run.id, at=plan.at)
            else:
                if run.subtask_id is None:
                    raise InvalidTaskTransition("Planned executor Run has no Subtask")
                subtask = by_id[run.subtask_id]
                subtask.queue(run.id, at=plan.at)
                uow.subtasks.save(subtask)
            self._persist_run_request(uow, task, run, at=plan.at, causation_id=plan.causation_id)
            persisted_runs.append(run)
        if plan.planned_runs:
            uow.tasks.save(task)
        return tuple(persisted_runs)

    @staticmethod
    def _normalize_at(value: datetime | None) -> datetime:
        if value is None:
            return utc_now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTaskTransition("Coordinated policy time must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _run_fingerprint(run: TaskRun) -> tuple[Any, ...]:
        return (
            run.id,
            run.task_id,
            run.status,
            run.subtask_id,
            run.role,
            run.agent_id,
            run.agent_version_id,
            run.agent_version_digest,
            run.queued_at,
            run.started_at,
            run.completed_at,
            run.pause_requested_at,
            run.paused_at,
            run.resumed_at,
            run.runtime_authority,
            run.runtime_version_id,
            run.runtime_execution_id,
            run.runtime_execution_intent_id,
        )

    @staticmethod
    def _task_matches_plan(task: Task, plan: CoordinatedSchedulePlan) -> bool:
        return (
            task.version == plan.task_version
            and task.status is plan.task_status
            and task.current_run_id == plan.task_current_run_id
            and task.plan_version == plan.plan_version
            and task.plan_digest == plan.plan_digest
        )

    @classmethod
    def _subtasks_match_plan(cls, subtasks: list[Subtask], plan: CoordinatedSchedulePlan) -> bool:
        by_id = {subtask.id: subtask for subtask in subtasks}
        for subtask_id, version, status, current_run_id, output in plan.subtask_snapshot:
            subtask = by_id.get(subtask_id)
            if subtask is None:
                return False
            if subtask_id == plan.hypothetical_subtask_id:
                if (
                    status is not SubtaskStatus.RUNNING
                    or current_run_id is None
                    or subtask.status is not SubtaskStatus.COMPLETED
                    or subtask.version != version + 1
                    or subtask.current_run_id != current_run_id
                    or subtask.output != _thaw_json(plan.hypothetical_output)
                ):
                    return False
            elif (
                subtask.version != version
                or subtask.status is not status
                or subtask.current_run_id != current_run_id
                or subtask.output != _thaw_json(output)
            ):
                return False
        return len(by_id) == len(plan.subtask_snapshot)

    @classmethod
    def _runs_match_plan(cls, runs: list[TaskRun], plan: CoordinatedSchedulePlan) -> bool:
        current = {run.id: run for run in runs}
        expected = {snapshot[0]: snapshot for snapshot in plan.run_snapshot}
        if set(current) != set(expected):
            return False
        target_id = None
        if plan.hypothetical_subtask_id is not None:
            for snapshot in plan.subtask_snapshot:
                if snapshot[0] == plan.hypothetical_subtask_id:
                    target_id = snapshot[3]
                    break
        for run_id, snapshot in expected.items():
            actual = current[run_id]
            if target_id == run_id:
                if (
                    snapshot[2] is not RunStatus.RUNNING
                    or actual.status is not RunStatus.SUCCEEDED
                    or actual.completed_at != plan.at
                    or actual.output != _thaw_json(plan.hypothetical_output)
                ):
                    return False
                continue
            if cls._run_fingerprint(actual) != snapshot:
                return False
        return True

    def _validate_apply_plan(
        self,
        uow: Any,
        task: Task | None,
        plan: CoordinatedSchedulePlan,
        subtasks: list[Subtask],
        runs: list[TaskRun],
    ) -> None:
        """Validate every plan invariant before the first business mutation."""
        if task is None or not isinstance(plan, CoordinatedSchedulePlan):
            raise InvalidTaskTransition("Coordinated schedule plan is invalid")
        if task.id != plan.task_id or task.execution_mode.value != "COORDINATED":
            raise InvalidTaskTransition("Coordinated schedule plan Task is invalid")
        if not isinstance(plan.cohort, AuthorityCohort):
            raise InvalidTaskTransition("Coordinated plan cohort is invalid")
        if (
            not isinstance(plan.planned_runs, tuple)
            or not isinstance(plan.ready_subtask_ids, tuple)
            or not isinstance(plan.planned_run_snapshot, tuple)
        ):
            raise InvalidTaskTransition("Coordinated plan collections are invalid")
        if plan.planned_runs != plan.planned_run_snapshot:
            raise InvalidTaskTransition("Coordinated plan Run specifications are stale")
        if type(plan.at) is not datetime or plan.at.tzinfo is None or plan.at.utcoffset() is None:
            raise InvalidTaskTransition("Coordinated plan policy time is invalid")
        if plan.causation_id is not None and type(plan.causation_id) is not UUID:
            raise InvalidTaskTransition("Coordinated plan causation ID is invalid")
        if plan.cohort.task_id != task.id or plan.cohort.tenant_id != task.tenant_id:
            raise InvalidTaskTransition("Coordinated plan cohort is not Task-bound")
        if (
            plan.hypothetical_subtask_id is not None
            and type(plan.hypothetical_subtask_id) is not UUID
        ):
            raise InvalidTaskTransition("Coordinated plan hypothetical target is invalid")
        if plan.hypothetical_output is not None and not isinstance(
            _thaw_json(plan.hypothetical_output), dict
        ):
            raise InvalidTaskTransition("Coordinated plan hypothetical output is invalid")
        if not isinstance(plan.subtask_snapshot, tuple) or any(
            not isinstance(snapshot, tuple) or len(snapshot) != 5
            for snapshot in plan.subtask_snapshot
        ):
            raise InvalidTaskTransition("Coordinated plan Subtask token is invalid")
        if any(
            type(snapshot[0]) is not UUID
            or type(snapshot[1]) is not int
            or not isinstance(snapshot[2], SubtaskStatus)
            for snapshot in plan.subtask_snapshot
        ):
            raise InvalidTaskTransition("Coordinated plan Subtask token is invalid")
        if not isinstance(plan.run_snapshot, tuple):
            raise InvalidTaskTransition("Coordinated plan Run token is invalid")
        snapshot_ids: list[UUID] = []
        for snapshot in plan.run_snapshot:
            if not isinstance(snapshot, tuple) or len(snapshot) != 18:
                raise InvalidTaskTransition("Coordinated plan Run token is invalid")
            (
                run_id,
                run_task_id,
                status,
                subtask_id,
                role,
                agent_id,
                agent_version_id,
                agent_version_digest,
                queued_at,
                started_at,
                completed_at,
                pause_requested_at,
                paused_at,
                resumed_at,
                runtime_authority,
                runtime_version_id,
                runtime_execution_id,
                runtime_execution_intent_id,
            ) = snapshot
            optional_uuids = (
                subtask_id,
                agent_version_id,
                runtime_version_id,
                runtime_execution_id,
                runtime_execution_intent_id,
            )
            optional_times = (
                started_at,
                completed_at,
                pause_requested_at,
                paused_at,
                resumed_at,
            )
            if (
                type(run_id) is not UUID
                or run_task_id != task.id
                or not isinstance(status, RunStatus)
                or (subtask_id is not None and type(subtask_id) is not UUID)
                or not isinstance(role, RunRole)
                or type(agent_id) is not str
                or not agent_id
                or any(value is not None and type(value) is not UUID for value in optional_uuids)
                or (agent_version_digest is not None and type(agent_version_digest) is not str)
                or type(queued_at) is not datetime
                or queued_at.tzinfo is None
                or queued_at.utcoffset() is None
                or any(
                    value is not None
                    and (
                        type(value) is not datetime
                        or value.tzinfo is None
                        or value.utcoffset() is None
                    )
                    for value in optional_times
                )
                or type(runtime_authority) is not str
                or runtime_authority not in {"legacy", "managed"}
            ):
                raise InvalidTaskTransition("Coordinated plan Run token is invalid")
            snapshot_ids.append(run_id)
        existing_ids = [run.id for run in runs]
        if any(type(run_id) is not UUID for run_id in existing_ids) or len(
            set(existing_ids)
        ) != len(existing_ids):
            raise InvalidTaskTransition("Coordinated plan existing Runs are invalid")
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise InvalidTaskTransition("Coordinated plan Run token contains duplicate Runs")
        if not self._subtasks_match_plan(subtasks, plan):
            raise InvalidTaskTransition("Coordinated schedule Subtask state is stale")
        by_id = {subtask.id: subtask for subtask in subtasks}
        ready_ids = set(plan.ready_subtask_ids)
        if len(ready_ids) != len(plan.ready_subtask_ids):
            raise InvalidTaskTransition("Coordinated plan contains duplicate ready Subtasks")
        for subtask_id in ready_ids:
            subtask = by_id.get(subtask_id)
            if subtask is None or subtask.status is not SubtaskStatus.BLOCKED:
                raise InvalidTaskTransition("Coordinated plan ready Subtask is stale")
        existing_ids = {run.id for run in runs}
        planned_ids = [run.id for run in plan.planned_runs]
        if any(type(value) is not UUID for value in planned_ids):
            raise InvalidTaskTransition("Coordinated plan Run identity is invalid")
        if len(set(planned_ids)) != len(planned_ids) or existing_ids.intersection(planned_ids):
            raise InvalidTaskTransition("Coordinated plan Run identity is not unique")
        supervisor_count = 0
        for planned in plan.planned_runs:
            if not isinstance(planned, PlannedRunSpec):
                raise InvalidTaskTransition("Coordinated plan Run specification is invalid")
            if (
                type(planned.id) is not UUID
                or planned.task_id != task.id
                or type(planned.thread_id) is not str
                or planned.thread_id != str(planned.id)
                or type(planned.revision_number) is not int
                or planned.revision_number < 0
                or planned.status is not RunStatus.QUEUED
                or planned.queued_at != plan.at
                or planned.runtime_authority != plan.cohort.runtime_authority
                or planned.runtime_version_id != plan.cohort.runtime_version_id
                or planned.comparison_mode != plan.cohort.comparison_mode
                or not planned.agent_id
            ):
                raise InvalidTaskTransition("Coordinated plan Run binding is invalid")
            if not isinstance(planned.role, RunRole):
                raise InvalidTaskTransition("Coordinated plan Run role is invalid")
            if planned.role is RunRole.SUPERVISOR:
                supervisor_count += 1
                if planned.subtask_id is not None:
                    raise InvalidTaskTransition("Planned Supervisor Run cannot bind a Subtask")
            elif planned.role is RunRole.EXECUTOR:
                bound = by_id.get(planned.subtask_id)
                if bound is None or (
                    planned.subtask_id not in ready_ids
                    and not (bound.status is SubtaskStatus.READY and bound.current_run_id is None)
                ):
                    raise InvalidTaskTransition("Planned executor Run is not a ready Subtask")
            else:
                raise InvalidTaskTransition("Coordinated plan Run role is invalid")
            if planned.agent_version_id is None or planned.agent_version_digest is None:
                raise InvalidTaskTransition("Planned Run Agent Version is incomplete")
            definition = uow.agent_definitions.get_by_name(
                task.tenant_id, planned.agent_id, for_update=True
            )
            if (
                definition is None
                or definition.lifecycle is not AgentDefinitionLifecycle.ACTIVE
                or definition.default_version_id != planned.agent_version_id
            ):
                raise InvalidTaskTransition("Planned Agent Definition is no longer active")
            version = uow.agent_versions.get(planned.agent_version_id, for_update=True)
            if (
                version is None
                or version.definition_id != definition.id
                or version.status is not AgentVersionStatus.PUBLISHED
                or version.content_digest != planned.agent_version_digest
            ):
                raise InvalidTaskTransition("Planned Agent Version is no longer available")
        if supervisor_count > 1 or (supervisor_count and len(plan.planned_runs) != 1):
            raise InvalidTaskTransition("Coordinated plan Supervisor cardinality is invalid")
        if plan.wait_for_budget:
            if plan.budget_rejection is None or plan.planned_runs:
                raise InvalidTaskTransition("Budget hold plan contains continuation Runs")
        # A scheduler may have admitted the first ready Subtasks and then hit a
        # budget boundary while planning the next one.  In that case the plan
        # carries both the admitted Runs and the rejection that stopped further
        # admission.  It is deliberately not a budget-hold transition: the
        # already-active/queued work remains authoritative, and the rejection
        # is only diagnostic for the caller.  The same shape is valid when an
        # active Run prevents a new admission (no planned Runs, no wait).

    @staticmethod
    def _plan_run_rejection(
        uow: Any,
        task: Task,
        existing_runs: list[TaskRun],
        planned_count: int,
        *,
        now: datetime,
    ) -> str | None:
        policy = task.budget
        if policy is None:
            return None
        if policy.deadline is not None and now >= policy.deadline:
            return "budget_deadline_exceeded"
        run_count = len(existing_runs) + planned_count
        if policy.max_runs is not None and run_count >= policy.max_runs:
            return "budget_run_limit_exhausted"
        queued_runs = sum(run.status is RunStatus.QUEUED for run in existing_runs) + planned_count
        if policy.max_tokens is not None and (
            task.settled_tokens
            + task.reserved_tokens
            + (queued_runs + 1) * policy.token_reservation_per_attempt
            > policy.max_tokens
        ):
            return "budget_token_limit_exhausted"
        if policy.max_cost_micros is not None and (
            task.settled_cost_micros
            + task.reserved_cost_micros
            + (queued_runs + 1) * policy.cost_reservation_micros_per_attempt
            > policy.max_cost_micros
        ):
            return "budget_cost_limit_exhausted"
        return None

    def _new_run(
        self,
        uow: Any,
        task: Task,
        agent_id: str,
        *,
        cohort: AuthorityCohort,
        at: datetime | None = None,
        **kwargs: Any,
    ) -> TaskRun:
        return self._authority_cohort_resolver.create_continuation_from_cohort_in_uow(
            uow, task, agent_id=agent_id, cohort=cohort, at=at, **kwargs
        )

    def work_item_input(self, uow: Any, task: Task, run: TaskRun) -> tuple[str, dict[str, Any]]:
        item = CanonicalWorkItemBuilder(self).build(task, run, uow=uow)
        return item.objective, item.input

    @staticmethod
    def _work_item_input_legacy(uow: Any, task: Task, run: TaskRun) -> tuple[str, dict[str, Any]]:
        subtasks = uow.subtasks.list_for_task(task.id)
        if run.role == RunRole.SUPERVISOR:
            return (
                f"Synthesize coordinated result: {task.objective}",
                {
                    "plan_version": task.plan_version,
                    "plan_digest": task.plan_digest,
                    "subtask_outputs": {
                        subtask.key: dict(subtask.output or {}) for subtask in subtasks
                    },
                },
            )
        if run.subtask_id is None:
            raise InvalidTaskTransition(f"Coordinated Run {run.id} has no Subtask binding")
        by_id = {subtask.id: subtask for subtask in subtasks}
        subtask = by_id.get(run.subtask_id)
        if subtask is None:
            raise InvalidTaskTransition(f"Run {run.id} references an unknown Subtask")
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        predecessor_ids = {
            dependency.predecessor_id
            for dependency in dependencies
            if dependency.successor_id == subtask.id
        }
        return (
            subtask.objective,
            {
                "subtask_input": dict(subtask.input),
                "dependency_outputs": {
                    by_id[predecessor_id].key: dict(by_id[predecessor_id].output or {})
                    for predecessor_id in sorted(
                        predecessor_ids, key=lambda value: by_id[value].key
                    )
                },
                "accepted_handoffs": [
                    handoff.execution_context()
                    for handoff in uow.handoffs.list_for_target(
                        subtask.id, status=HandoffStatus.ACCEPTED
                    )
                ],
            },
        )

    def _resolve_subtask_agent(
        self,
        uow: Any,
        tenant_id: str,
        subtask: Subtask,
        accepted_handoff: Handoff | None = None,
    ) -> tuple[str, AgentVersion]:
        required = set(subtask.required_capabilities)
        if accepted_handoff is not None:
            return self.resolve_named_agent(
                uow, tenant_id, accepted_handoff.target_agent_id, required
            )
        if subtask.preferred_agent_id is not None:
            return self.resolve_named_agent(uow, tenant_id, subtask.preferred_agent_id, required)
        candidates: list[tuple[str, AgentVersion]] = []
        definitions = uow.agent_definitions.list(tenant_id=tenant_id, limit=1_000, offset=0)
        for definition in definitions:
            if (
                definition.lifecycle != AgentDefinitionLifecycle.ACTIVE
                or definition.default_version_id is None
            ):
                continue
            version = uow.agent_versions.get(definition.default_version_id)
            if self._version_satisfies(version, required):
                candidates.append((definition.name, version))
        if not candidates:
            capabilities = ", ".join(sorted(required))
            raise AgentUnavailable(
                f"No published Agent Version satisfies Subtask {subtask.key}: {capabilities}"
            )
        return min(candidates, key=lambda value: (value[0], str(value[1].id)))

    @staticmethod
    def _accepted_by_target(uow: Any, task_id: Any) -> dict[Any, Handoff]:
        return {
            handoff.target_subtask_id: handoff
            for handoff in uow.handoffs.list_for_task(task_id)
            if handoff.status == HandoffStatus.ACCEPTED
        }

    @classmethod
    def resolve_named_agent(
        cls,
        uow: Any,
        tenant_id: str,
        configured_name: str,
        required: set[str],
    ) -> tuple[str, AgentVersion]:
        return resolve_agent_by_name(uow, tenant_id, configured_name, required)

    @staticmethod
    def _version_satisfies(version: AgentVersion | None, required: set[str]) -> bool:
        return agent_version_satisfies(version, required)

    @staticmethod
    def _persist_run_request(
        uow: Any,
        task: Task,
        run: TaskRun,
        *,
        at: datetime | None = None,
        causation_id: UUID | None = None,
    ) -> None:
        uow.runs.add(run)
        uow.outbox.add(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
                at=at,
                causation_id=causation_id,
            )
        )


__all__ = ["CoordinatedSchedulePlan", "CoordinatedScheduler"]
