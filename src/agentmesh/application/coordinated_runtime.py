"""Transaction-local locking projection for coordinated Runtime aggregates.

This module intentionally contains no coordinated behavior writer.  It only
expands and locks the complete aggregate in the one order shared by later
barrier commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.authority_cohorts import AuthorityCohort
from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    RuntimeHandleSnapshot,
)
from agentmesh.domain.coordination import (
    COORDINATION_USER_CANCEL_REQUESTED,
    TERMINAL_SUBTASK_STATUSES,
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    SubtaskCancellationSource,
    SubtaskStatus,
    classify_runtime_boundary,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeLifecycleIntent,
    RuntimeVersion,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Subtask,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)


@dataclass(frozen=True)
class CoordinatedRuntimeAggregate:
    """Immutable projection valid only while its owning UoW is open."""

    task: Task
    active_drain: CoordinationRuntimeDrain | None
    cohort: AuthorityCohort
    runtime_versions: Mapping[UUID, RuntimeVersion]
    subtasks: tuple[Subtask, ...]
    runs: tuple[TaskRun, ...]
    latest_attempts: Mapping[UUID, TaskAttempt | None]
    executions: tuple[RuntimeExecution, ...]
    assignment_snapshots: tuple[RuntimeAssignmentSnapshot, ...]
    handle_snapshots: tuple[RuntimeHandleSnapshot, ...]
    lifecycle_operations: tuple[RuntimeLifecycleIntent, ...]
    integrity_incidents: tuple[RuntimeIntegrityIncident, ...]
    boundary_classifications: Mapping[UUID, CoordinationRuntimeBoundary]

    @property
    def executions_by_run(self) -> Mapping[UUID, tuple[RuntimeExecution, ...]]:
        grouped: dict[UUID, list[RuntimeExecution]] = {}
        for execution in self.executions:
            grouped.setdefault(execution.run_id, []).append(execution)
        return MappingProxyType({key: tuple(value) for key, value in grouped.items()})

    @property
    def assignment_snapshots_by_execution(
        self,
    ) -> Mapping[UUID, RuntimeAssignmentSnapshot]:
        return MappingProxyType(
            {value.runtime_execution_id: value for value in self.assignment_snapshots}
        )

    @property
    def handle_snapshots_by_execution(self) -> Mapping[UUID, RuntimeHandleSnapshot]:
        return MappingProxyType(
            {value.runtime_execution_id: value for value in self.handle_snapshots}
        )

    @property
    def lifecycle_operations_by_execution(
        self,
    ) -> Mapping[UUID, tuple[RuntimeLifecycleIntent, ...]]:
        grouped: dict[UUID, list[RuntimeLifecycleIntent]] = {}
        for value in self.lifecycle_operations:
            grouped.setdefault(value.runtime_execution_id, []).append(value)
        return MappingProxyType({key: tuple(value) for key, value in grouped.items()})

    @property
    def integrity_incidents_by_execution(
        self,
    ) -> Mapping[UUID, tuple[RuntimeIntegrityIncident, ...]]:
        grouped: dict[UUID, list[RuntimeIntegrityIncident]] = {}
        for value in self.integrity_incidents:
            grouped.setdefault(value.runtime_execution_id, []).append(value)
        return MappingProxyType({key: tuple(value) for key, value in grouped.items()})


class CoordinatedRuntimeAggregateLocker:
    """Acquire one deterministic, tenant-scoped coordinated aggregate lock set."""

    def lock(self, uow: Any, *, tenant_id: str, task_id: UUID) -> CoordinatedRuntimeAggregate:
        if type(tenant_id) is not str or not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise RuntimeExecutionConflict("Coordinated aggregate tenant is invalid")
        if type(task_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated aggregate Task identity is invalid")

        task = uow.tasks.get(task_id, for_update=True)
        if task is None or task.id != task_id or task.tenant_id != tenant_id:
            raise RuntimeExecutionConflict("Coordinated aggregate Task is unavailable")
        if task.execution_mode is not TaskExecutionMode.COORDINATED:
            raise RuntimeExecutionConflict("Coordinated aggregate Task is not COORDINATED")
        return self.lock_after_task(uow, task, tenant_id=tenant_id, task_id=task_id)

    def lock_after_task(
        self,
        uow: Any,
        locked_task: Task,
        *,
        tenant_id: str,
        task_id: UUID,
    ) -> CoordinatedRuntimeAggregate:
        """Expand and lock the aggregate after the caller locked its Task.

        The supplied Task is the caller's already-locked identity-map object.
        This helper deliberately does not read the Task repository, open a UoW,
        or commit; callers must keep the same transaction open across the call.
        """
        if type(tenant_id) is not str or not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise RuntimeExecutionConflict("Coordinated aggregate tenant is invalid")
        if type(task_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated aggregate Task identity is invalid")
        if (
            type(locked_task) is not Task
            or type(locked_task.id) is not UUID
            or locked_task.id != task_id
            or locked_task.tenant_id != tenant_id
            or locked_task.execution_mode is not TaskExecutionMode.COORDINATED
            or type(locked_task.version) is not int
            or locked_task.version <= 0
        ):
            raise RuntimeExecutionConflict("Coordinated aggregate Task is invalid")
        task = locked_task
        task_version = task.version

        active_drain = uow.coordination_runtime_drains.get_active_for_task(
            task_id, tenant_id=tenant_id, for_update=True
        )
        self._validate_drain(active_drain, task, tenant_id)

        # Discovery is allowed only after the Task lock.  Runtime versions are
        # then locked before Subtasks and Runs, as required by the fixed order.
        discovered_subtasks = tuple(uow.subtasks.list_for_task(task_id))
        discovered_runs = tuple(uow.runs.list_for_task(task_id))
        self._validate_membership(task, discovered_subtasks, discovered_runs)
        version_ids = self._cohort_version_ids(discovered_runs)
        runtime_versions: dict[UUID, RuntimeVersion] = {}
        for version_id in sorted(version_ids, key=str):
            version = uow.runtimes.get_version(
                version_id, tenant_id=tenant_id, for_update=True
            )
            if type(version) is not RuntimeVersion or version.id != version_id:
                raise RuntimeExecutionConflict("Pinned Runtime Version is unavailable")
            runtime_versions[version_id] = version

        subtasks = tuple(
            sorted(
                uow.subtasks.list_for_task(task_id, for_update=True),
                key=lambda value: value.id,
            )
        )
        runs = tuple(
            sorted(
                uow.runs.list_for_task(task_id, for_update=True),
                key=lambda value: value.id,
            )
        )
        self._validate_membership(task, subtasks, runs)
        if {value.id for value in subtasks} != {value.id for value in discovered_subtasks}:
            raise RuntimeExecutionConflict("Coordinated aggregate Subtask membership changed")
        if {value.id for value in runs} != {value.id for value in discovered_runs}:
            raise RuntimeExecutionConflict("Coordinated aggregate Run membership changed")
        cohort = self._resolve_cohort(task, runs, runtime_versions)

        latest_attempts: dict[UUID, TaskAttempt | None] = {}
        for run in runs:
            latest = uow.attempts.latest_for_run(
                run.id, for_update=True
            )
            if latest is not None and latest.run_id != run.id:
                raise RuntimeExecutionConflict("Latest Attempt is bound to another Run")
            latest_attempts[run.id] = latest

        executions_by_run: dict[UUID, tuple[RuntimeExecution, ...]] = {}
        execution_values: list[RuntimeExecution] = []
        for run in runs:
            values = tuple(
                sorted(
                    uow.runtimes.list_executions_for_run(
                        run.id, tenant_id=tenant_id, for_update=True
                    ),
                    key=lambda value: value.id,
                )
            )
            for execution in values:
                if (
                    execution.tenant_id != tenant_id
                    or execution.run_id != run.id
                    or execution.runtime_version_id != run.runtime_version_id
                ):
                    raise RuntimeExecutionConflict("RuntimeExecution binding is invalid")
            executions_by_run[run.id] = values
            execution_values.extend(values)
        if len({value.id for value in execution_values}) != len(execution_values):
            raise RuntimeExecutionConflict("RuntimeExecution identity is duplicated")

        assignment_values: list[RuntimeAssignmentSnapshot] = []
        handle_values: list[RuntimeHandleSnapshot] = []
        lifecycle_values: list[RuntimeLifecycleIntent] = []
        incident_values: list[RuntimeIntegrityIncident] = []
        dependent_ids: dict[
            UUID, tuple[UUID | None, UUID | None, tuple[UUID, ...], tuple[UUID, ...]]
        ] = {}
        for execution in execution_values:
            assignment = uow.runtimes.get_assignment_snapshot(
                execution.id, tenant_id=tenant_id, for_update=True
            )
            if assignment is not None:
                self._validate_dependent(assignment, execution, tenant_id)
                assignment_values.append(assignment)
            handle = uow.runtimes.get_handle_snapshot(
                execution.id, tenant_id=tenant_id, for_update=True
            )
            if handle is not None:
                self._validate_dependent(handle, execution, tenant_id)
                handle_values.append(handle)
            lifecycle = tuple(
                sorted(
                    uow.runtimes.list_lifecycle_operations(
                        execution.id, tenant_id=tenant_id, for_update=True
                    ),
                    key=lambda value: value.id,
                )
            )
            for value in lifecycle:
                self._validate_dependent(value, execution, tenant_id)
            lifecycle_values.extend(lifecycle)
            incidents = tuple(
                sorted(
                    uow.runtimes.list_integrity_incidents_for_execution(
                        execution.id, tenant_id=tenant_id, for_update=True
                    ),
                    key=lambda value: value.id,
                )
            )
            for value in incidents:
                self._validate_dependent(value, execution, tenant_id)
            incident_values.extend(incidents)
            dependent_ids[execution.id] = (
                assignment.id if assignment is not None else None,
                handle.id if handle is not None else None,
                tuple(value.id for value in lifecycle),
                tuple(value.id for value in incidents),
            )

        boundary_classifications = self._classify_current_runs(
            task, subtasks, runs, latest_attempts, executions_by_run
        )
        self._revalidate_membership(
            uow,
            task,
            active_drain,
            subtasks,
            runs,
            latest_attempts,
            execution_values,
            dependent_ids,
            tenant_id,
        )
        if task.version != task_version:
            raise RuntimeExecutionConflict("Coordinated aggregate Task version changed")
        return CoordinatedRuntimeAggregate(
            task=task,
            active_drain=active_drain,
            cohort=cohort,
            runtime_versions=MappingProxyType(runtime_versions),
            subtasks=subtasks,
            runs=runs,
            latest_attempts=MappingProxyType(latest_attempts),
            executions=tuple(execution_values),
            assignment_snapshots=tuple(assignment_values),
            handle_snapshots=tuple(handle_values),
            lifecycle_operations=tuple(lifecycle_values),
            integrity_incidents=tuple(incident_values),
            boundary_classifications=MappingProxyType(boundary_classifications),
        )

    @staticmethod
    def _validate_drain(
        drain: CoordinationRuntimeDrain | None, task: Task, tenant_id: str
    ) -> None:
        if drain is not None and (
            drain.tenant_id != tenant_id
            or drain.task_id != task.id
            or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
        ):
            raise RuntimeExecutionConflict("Coordinated aggregate drain binding is invalid")

    @staticmethod
    def _validate_membership(
        task: Task, subtasks: tuple[Subtask, ...], runs: tuple[TaskRun, ...]
    ) -> None:
        if any(value.task_id != task.id for value in subtasks + runs):
            raise RuntimeExecutionConflict("Coordinated aggregate membership is invalid")
        subtask_ids = {value.id for value in subtasks}
        for run in runs:
            if run.subtask_id is not None and run.subtask_id not in subtask_ids:
                raise RuntimeExecutionConflict("Task Run references an unknown Subtask")

    @staticmethod
    def _cohort_version_ids(runs: tuple[TaskRun, ...]) -> set[UUID]:
        if not runs:
            return set()
        authorities = {run.runtime_authority for run in runs}
        comparisons = {run.comparison_mode for run in runs}
        if len(authorities) != 1 or len(comparisons) != 1:
            raise RuntimeExecutionConflict("Task contains mixed Runtime cohorts")
        authority = next(iter(authorities))
        comparison = next(iter(comparisons))
        version_ids: set[UUID] = set()
        for run in runs:
            if run.runtime_authority == "managed":
                if run.runtime_version_id is None:
                    raise RuntimeExecutionConflict("Managed Run has no Runtime Version")
                version_ids.add(run.runtime_version_id)
            elif run.runtime_authority != "legacy":
                raise RuntimeExecutionConflict("Run Runtime authority is invalid")
            if run.comparison_mode not in {"off", "deterministic_shadow"}:
                raise RuntimeExecutionConflict("Run Runtime comparison mode is invalid")
            if run.comparison_mode == "deterministic_shadow":
                if run.runtime_version_id is None:
                    raise RuntimeExecutionConflict("Shadow Run has no Runtime Version")
                version_ids.add(run.runtime_version_id)
        pinned = {run.runtime_version_id for run in runs}
        if authority == "managed" and (
            comparison != "off" or len(pinned) != 1 or None in pinned
        ):
            raise RuntimeExecutionConflict("Task contains mixed managed Runtime cohorts")
        if authority == "legacy" and comparison == "off" and any(
            version is not None for version in pinned
        ):
            raise RuntimeExecutionConflict("Legacy cohort contains a Runtime Version")
        if authority == "legacy" and comparison == "off" and any(
            run.runtime_execution_id is not None or run.runtime_execution_intent_id is not None
            for run in runs
        ):
            raise RuntimeExecutionConflict("Legacy cohort contains Runtime metadata")
        if authority == "legacy" and comparison == "deterministic_shadow" and (
            len(pinned) != 1 or None in pinned
        ):
            raise RuntimeExecutionConflict("Shadow cohort contains mixed Runtime Versions")
        return version_ids

    @staticmethod
    def _resolve_cohort(
        task: Task, runs: tuple[TaskRun, ...], versions: Mapping[UUID, RuntimeVersion]
    ) -> AuthorityCohort:
        if not runs:
            return AuthorityCohort("legacy", None, "off", task_id=task.id, tenant_id=task.tenant_id)
        authorities = {run.runtime_authority for run in runs}
        comparisons = {run.comparison_mode for run in runs}
        version_ids = {run.runtime_version_id for run in runs}
        if len(authorities) != 1 or len(comparisons) != 1:
            raise RuntimeExecutionConflict("Task contains mixed Runtime cohorts")
        authority = next(iter(authorities))
        comparison = next(iter(comparisons))
        if authority == "managed":
            if comparison != "off" or len(version_ids) != 1 or None in version_ids:
                raise RuntimeExecutionConflict("Task contains mixed managed Runtime cohorts")
            version_id = next(iter(version_ids))
            if version_id not in versions:
                raise RuntimeExecutionConflict("Managed Runtime Version is unavailable")
            return AuthorityCohort(
                "managed", version_id, "off", task_id=task.id, tenant_id=task.tenant_id
            )
        if authority != "legacy":
            raise RuntimeExecutionConflict("Task Runtime authority is invalid")
        if comparison == "off" and any(version is not None for version in version_ids):
            raise RuntimeExecutionConflict("Legacy cohort contains a Runtime Version")
        if comparison == "deterministic_shadow" and (
            len(version_ids) != 1 or None in version_ids
        ):
            raise RuntimeExecutionConflict("Shadow cohort contains mixed Runtime Versions")
        version_id = next(iter(version_ids)) if comparison == "deterministic_shadow" else None
        return AuthorityCohort(
            "legacy", version_id, comparison, task_id=task.id, tenant_id=task.tenant_id
        )

    @staticmethod
    def _validate_dependent(value: Any, execution: RuntimeExecution, tenant_id: str) -> None:
        if (
            value.tenant_id != tenant_id
            or value.runtime_execution_id != execution.id
        ):
            raise RuntimeExecutionConflict("Runtime dependent row binding is invalid")

    @staticmethod
    def _classify_current_runs(
        task: Task,
        subtasks: tuple[Subtask, ...],
        runs: tuple[TaskRun, ...],
        latest_attempts: Mapping[UUID, TaskAttempt | None],
        executions_by_run: Mapping[UUID, tuple[RuntimeExecution, ...]],
    ) -> dict[UUID, CoordinationRuntimeBoundary]:
        by_run = {run.id: run for run in runs}
        result: dict[UUID, CoordinationRuntimeBoundary] = {}
        for subtask in subtasks:
            if subtask.current_run_id is None:
                continue
            run = by_run.get(subtask.current_run_id)
            if run is None or run.subtask_id != subtask.id or run.role is not RunRole.EXECUTOR:
                raise RuntimeExecutionConflict("Subtask current Run binding is invalid")
            if run.runtime_authority == "managed":
                if CoordinatedRuntimeAggregateLocker._is_provider_free_terminal(
                    task=task,
                    subtask=subtask,
                    run=run,
                    latest_attempt=latest_attempts[run.id],
                    executions=executions_by_run[run.id],
                ):
                    continue
                try:
                    result[run.id] = classify_runtime_boundary(
                        subtask=subtask,
                        run=run,
                        latest_attempt=latest_attempts[run.id],
                        executions=executions_by_run[run.id],
                    )
                except (InvalidTaskInput, InvalidTaskTransition) as exc:
                    raise RuntimeExecutionConflict(
                        "Managed coordinated Run boundary is invalid"
                    ) from exc
        for run in runs:
            if run.subtask_id is not None:
                if run.role is not RunRole.EXECUTOR:
                    raise RuntimeExecutionConflict("Only Executor Runs may bind a Subtask")
                subtask = next(
                    (value for value in subtasks if value.id == run.subtask_id), None
                )
                if subtask is None:
                    raise RuntimeExecutionConflict("Run/Subtask binding is invalid")
                if (
                    subtask.current_run_id == run.id
                    and run.runtime_authority == "managed"
                    and run.id not in result
                    and not CoordinatedRuntimeAggregateLocker._is_provider_free_terminal(
                        task=task,
                        subtask=subtask,
                        run=run,
                        latest_attempt=latest_attempts[run.id],
                        executions=executions_by_run[run.id],
                    )
                ):
                    raise RuntimeExecutionConflict("Run/Subtask current binding is invalid")
        if task.current_run_id is not None:
            current = [run for run in runs if run.id == task.current_run_id]
            if (
                len(current) != 1
                or current[0].role is not RunRole.SUPERVISOR
                or current[0].subtask_id is not None
            ):
                raise RuntimeExecutionConflict("Task current Supervisor Run binding is invalid")
            run = current[0]
            if run.runtime_authority == "managed":
                if CoordinatedRuntimeAggregateLocker._is_provider_free_terminal(
                    task=task,
                    subtask=None,
                    run=run,
                    latest_attempt=latest_attempts[run.id],
                    executions=executions_by_run[run.id],
                ):
                    return result
                result[run.id] = CoordinatedRuntimeAggregateLocker._classify_supervisor(
                    task=task,
                    subtasks=subtasks,
                    run=run,
                    latest_attempt=latest_attempts[run.id],
                    executions=executions_by_run[run.id],
                )
        return result

    @staticmethod
    def _is_provider_free_terminal(
        *,
        task: Task,
        subtask: Subtask | None,
        run: TaskRun,
        latest_attempt: TaskAttempt | None,
        executions: tuple[RuntimeExecution, ...],
    ) -> bool:
        """Recognize the durable local terminal created before provider contact."""
        if run.runtime_authority != "managed":
            return False
        if run.runtime_execution_id is None:
            execution_safe = not executions
        else:
            execution_safe = (
                latest_attempt is not None
                and len(executions) == 1
                and executions[0].id == run.runtime_execution_id
                and executions[0].phase is RuntimeExecutionPhase.CANCELED
                and executions[0].current_owner_attempt_id == latest_attempt.id
                and executions[0].current_fencing_token == latest_attempt.fencing_token
            )
        if not execution_safe:
            return False
        provider_free_cancel = (
            run.status is RunStatus.CANCELED
            and run.output is None
            and run.error == COORDINATION_USER_CANCEL_REQUESTED
            and (
                (
                    latest_attempt is None
                    and run.started_at is None
                    and run.runtime_execution_id is None
                )
                or (
                    latest_attempt is not None
                    and latest_attempt.status is AttemptStatus.CANCELED
                    and latest_attempt.error == COORDINATION_USER_CANCEL_REQUESTED
                )
            )
        )
        if provider_free_cancel:
            if run.role is RunRole.EXECUTOR:
                expected_drain_id = uuid5(
                    NAMESPACE_URL,
                    f"coordination-runtime-drain:{task.tenant_id}:{task.id}",
                )
                return (
                    subtask is not None
                    and run.subtask_id == subtask.id
                    and subtask.current_run_id == run.id
                    and subtask.status is SubtaskStatus.CANCELED
                    and subtask.output is None
                    and subtask.error == COORDINATION_USER_CANCEL_REQUESTED
                    and subtask.cancellation_source
                    is SubtaskCancellationSource.CONTROL_DRAIN
                    and subtask.canceled_by_drain_id == expected_drain_id
                )
            # A locally canceled Supervisor is historical after its Task pointer
            # is released, so it needs no current-boundary classification.
            return False
        if latest_attempt is None:
            return False
        if (
            run.status is not RunStatus.FAILED
            or latest_attempt.status is not AttemptStatus.FAILED
            or type(run.error) is not str
            or not run.error
            or run.error != latest_attempt.error
        ):
            return False
        if run.role is RunRole.EXECUTOR:
            return (
                subtask is not None
                and run.subtask_id == subtask.id
                and subtask.current_run_id == run.id
                and subtask.status is SubtaskStatus.FAILED
                and subtask.error == run.error
            )
        return (
            run.role is RunRole.SUPERVISOR
            and subtask is None
            and run.subtask_id is None
            and task.current_run_id == run.id
            and task.status is TaskStatus.FAILED
            and task.error == run.error
        )

    @staticmethod
    def _classify_supervisor(
        *,
        task: Task,
        subtasks: tuple[Subtask, ...],
        run: TaskRun,
        latest_attempt: TaskAttempt | None,
        executions: tuple[RuntimeExecution, ...],
    ) -> CoordinationRuntimeBoundary:
        if (
            run.role is not RunRole.SUPERVISOR
            or run.subtask_id is not None
            or run.runtime_authority != "managed"
            or run.task_id != task.id
            or task.current_run_id != run.id
            or type(run.runtime_execution_intent_id) is not UUID
            or any(subtask.status not in TERMINAL_SUBTASK_STATUSES for subtask in subtasks)
        ):
            raise RuntimeExecutionConflict("Managed Supervisor boundary ownership is invalid")
        if latest_attempt is not None and (
            type(latest_attempt) is not TaskAttempt or latest_attempt.run_id != run.id
        ):
            raise RuntimeExecutionConflict("Managed Supervisor boundary Attempt is invalid")
        if any(type(value) is not RuntimeExecution for value in executions) or len(
            {value.id for value in executions}
        ) != len(executions):
            raise RuntimeExecutionConflict("Managed Supervisor executions are invalid")

        empty_task = (
            task.status is TaskStatus.RUNNING
            and task.output is None
            and task.candidate_output is None
            and task.error is None
            and task.budget_exhausted_reason is None
        )
        empty_run = run.output is None and run.error is None
        if run.status is RunStatus.QUEUED:
            if (
                not empty_task
                or not empty_run
                or latest_attempt is not None
                or run.runtime_execution_id is not None
                or executions
            ):
                raise RuntimeExecutionConflict("Queued managed Supervisor boundary is inconsistent")
            return CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
        if latest_attempt is None:
            raise RuntimeExecutionConflict("Managed Supervisor boundary lacks an Attempt")
        known_terminal = {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        parked = {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}
        if run.runtime_execution_id is None:
            if (
                not empty_task
                or not empty_run
                or run.status is not RunStatus.RUNNING
                or latest_attempt.status is not AttemptStatus.RUNNING
                or executions
            ):
                raise RuntimeExecutionConflict(
                    "Managed Supervisor boundary lacks a Runtime execution"
                )
            return CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
        if len(executions) != 1:
            raise RuntimeExecutionConflict("Managed Supervisor execution state is ambiguous")
        bound = [value for value in executions if value.id == run.runtime_execution_id]
        if len(bound) != 1 or run.runtime_execution_intent_id != run.runtime_execution_id:
            raise RuntimeExecutionConflict("Managed Supervisor execution binding is invalid")
        execution = bound[0]
        if (
            execution.tenant_id != task.tenant_id
            or execution.run_id != run.id
            or execution.runtime_version_id != run.runtime_version_id
            or execution.current_owner_attempt_id != latest_attempt.id
            or execution.current_fencing_token != latest_attempt.fencing_token
        ):
            raise RuntimeExecutionConflict("Managed Supervisor execution ownership is invalid")
        if execution.phase in parked:
            if (
                task.status is not TaskStatus.RECONCILIATION_REQUIRED
                or task.output is not None
                or task.candidate_output is not None
                or task.error != "coordination.runtime_reconciliation_required"
                or task.budget_exhausted_reason is not None
                or run.status is not RunStatus.RECONCILIATION_REQUIRED
                or run.output is not None
                or latest_attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
                or type(run.error) is not str
                or not run.error
                or run.error != latest_attempt.error
            ):
                raise RuntimeExecutionConflict("Parked managed Supervisor boundary is inconsistent")
            return CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
        if execution.phase in known_terminal:
            local = (latest_attempt.status, run.status, task.status)
            succeeded = (
                execution.phase is RuntimeExecutionPhase.SUCCEEDED
                and local
                == (AttemptStatus.SUCCEEDED, RunStatus.SUCCEEDED, TaskStatus.COMPLETED)
                and type(run.output) is dict
                and task.output == run.output
                and task.candidate_output is None
                and task.error is None
                and run.error is None
                and latest_attempt.error is None
                and task.budget_exhausted_reason is None
            )
            failed = (
                (
                    execution.phase
                    in {RuntimeExecutionPhase.FAILED, RuntimeExecutionPhase.TIMED_OUT}
                    or (
                        execution.phase is RuntimeExecutionPhase.CANCELED
                        and local
                        == (AttemptStatus.FAILED, RunStatus.FAILED, TaskStatus.FAILED)
                    )
                )
                and local == (AttemptStatus.FAILED, RunStatus.FAILED, TaskStatus.FAILED)
                and task.output is None
                and task.candidate_output is None
                and run.output is None
                and type(task.error) is str
                and bool(task.error)
                and task.error == run.error == latest_attempt.error
                and task.budget_exhausted_reason is None
            )
            canceled = (
                execution.phase is RuntimeExecutionPhase.CANCELED
                and local
                == (AttemptStatus.CANCELED, RunStatus.CANCELED, TaskStatus.CANCELED)
                and task.output is None
                and task.candidate_output is None
                and task.error is None
                and run.output is None
                and type(run.error) is str
                and bool(run.error)
                and run.error == latest_attempt.error
                and task.budget_exhausted_reason is None
            )
            if not (succeeded or failed or canceled):
                raise RuntimeExecutionConflict(
                    "Terminal managed Supervisor boundary is inconsistent"
                )
            return CoordinationRuntimeBoundary.KNOWN_TERMINAL
        if (
            not empty_task
            or not empty_run
            or run.status is not RunStatus.RUNNING
            or latest_attempt.status is not AttemptStatus.RUNNING
        ):
            raise RuntimeExecutionConflict("Active managed Supervisor boundary is inconsistent")
        if execution.phase is RuntimeExecutionPhase.PREPARED:
            return CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
        if execution.phase in {
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.ACCEPTED,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.WAITING_INPUT,
            RuntimeExecutionPhase.WAITING_APPROVAL,
            RuntimeExecutionPhase.PAUSE_REQUESTED,
            RuntimeExecutionPhase.PAUSED,
            RuntimeExecutionPhase.CANCEL_REQUESTED,
        }:
            return CoordinationRuntimeBoundary.CROSSED_ACTIVE
        raise RuntimeExecutionConflict("Managed Supervisor Runtime phase is invalid")

    def _revalidate_membership(
        self,
        uow: Any,
        task: Task,
        active_drain: CoordinationRuntimeDrain | None,
        subtasks: tuple[Subtask, ...],
        runs: tuple[TaskRun, ...],
        latest_attempts: Mapping[UUID, TaskAttempt | None],
        executions: tuple[RuntimeExecution, ...],
        dependent_ids: Mapping[
            UUID, tuple[UUID | None, UUID | None, tuple[UUID, ...], tuple[UUID, ...]]
        ],
        tenant_id: str,
    ) -> None:
        current_drain = uow.coordination_runtime_drains.get_active_for_task(
            task.id, tenant_id=tenant_id, for_update=False
        )
        if self._drain_identity(current_drain) != self._drain_identity(active_drain):
            raise RuntimeExecutionConflict("Coordinated aggregate drain changed")
        current_subtasks = tuple(uow.subtasks.list_for_task(task.id))
        current_runs = tuple(uow.runs.list_for_task(task.id))
        if {value.id for value in current_subtasks} != {value.id for value in subtasks}:
            raise RuntimeExecutionConflict("Coordinated aggregate Subtask membership changed")
        if {value.id for value in current_runs} != {value.id for value in runs}:
            raise RuntimeExecutionConflict("Coordinated aggregate Run membership changed")
        current_subtasks_by_id = {value.id: value for value in current_subtasks}
        for subtask in subtasks:
            if current_subtasks_by_id[subtask.id].current_run_id != subtask.current_run_id:
                raise RuntimeExecutionConflict("Subtask current Run binding changed")
        current_runs_by_id = {value.id: value for value in current_runs}
        for run in runs:
            current = current_runs_by_id[run.id]
            if (
                current.subtask_id != run.subtask_id
                or self._run_guard(current) != self._run_guard(run)
            ):
                raise RuntimeExecutionConflict("Run Subtask binding changed")
        current_executions: dict[UUID, tuple[RuntimeExecution, ...]] = {}
        for run in runs:
            current_attempt = uow.attempts.latest_for_run(run.id, for_update=False)
            locked_attempt = latest_attempts[run.id]
            if (current_attempt.id if current_attempt else None) != (
                locked_attempt.id if locked_attempt else None
            ):
                raise RuntimeExecutionConflict("Latest Attempt changed")
            if current_attempt is not None and locked_attempt is not None and (
                self._attempt_guard(current_attempt) != self._attempt_guard(locked_attempt)
            ):
                raise RuntimeExecutionConflict("Latest Attempt owner changed")
            current_executions[run.id] = tuple(
                sorted(
                    uow.runtimes.list_executions_for_run(
                        run.id, tenant_id=tenant_id, for_update=False
                    ),
                    key=lambda value: value.id,
                )
            )
            for current_value in current_executions[run.id]:
                if (
                    current_value.tenant_id != tenant_id
                    or current_value.run_id != run.id
                    or current_value.runtime_version_id != run.runtime_version_id
                ):
                    raise RuntimeExecutionConflict("RuntimeExecution binding changed")
        locked_by_run: dict[UUID, list[UUID]] = {}
        for execution in executions:
            locked_by_run.setdefault(execution.run_id, []).append(execution.id)
        for run in runs:
            current_values = current_executions[run.id]
            locked_ids = locked_by_run.get(run.id, [])
            if [value.id for value in current_values] != locked_ids:
                raise RuntimeExecutionConflict("RuntimeExecution membership changed")
            locked_values = {
                value.id: value for value in executions if value.run_id == run.id
            }
            for current_value in current_values:
                locked_value = locked_values[current_value.id]
                if self._execution_guard(current_value) != self._execution_guard(locked_value):
                    raise RuntimeExecutionConflict("RuntimeExecution state changed")
        for execution in executions:
            assignment = uow.runtimes.get_assignment_snapshot(
                execution.id, tenant_id=tenant_id, for_update=False
            )
            handle = uow.runtimes.get_handle_snapshot(
                execution.id, tenant_id=tenant_id, for_update=False
            )
            lifecycle = uow.runtimes.list_lifecycle_operations(
                execution.id, tenant_id=tenant_id, for_update=False
            )
            incidents = uow.runtimes.list_integrity_incidents_for_execution(
                execution.id, tenant_id=tenant_id, for_update=False
            )
            if assignment is not None:
                self._validate_dependent(assignment, execution, tenant_id)
            if handle is not None:
                self._validate_dependent(handle, execution, tenant_id)
            for value in lifecycle:
                self._validate_dependent(value, execution, tenant_id)
            for value in incidents:
                self._validate_dependent(value, execution, tenant_id)
            expected = dependent_ids[execution.id]
            if (
                (assignment.id if assignment else None) != expected[0]
                or (handle.id if handle else None) != expected[1]
                or tuple(sorted(value.id for value in lifecycle)) != expected[2]
                or tuple(sorted(value.id for value in incidents)) != expected[3]
            ):
                raise RuntimeExecutionConflict("Runtime dependent membership changed")

    @staticmethod
    def _drain_identity(value: CoordinationRuntimeDrain | None) -> tuple[Any, ...] | None:
        if value is None:
            return None
        return value.id, value.version, value.tenant_id, value.task_id, value.status

    @staticmethod
    def _run_guard(value: TaskRun) -> tuple[Any, ...]:
        return (
            value.task_id,
            value.subtask_id,
            value.role,
            value.runtime_authority,
            value.comparison_mode,
            value.runtime_version_id,
            value.runtime_execution_intent_id,
            value.runtime_execution_id,
            value.status,
        )

    @staticmethod
    def _attempt_guard(value: TaskAttempt) -> tuple[Any, ...]:
        return value.run_id, value.fencing_token, value.status, value.lease_token

    @staticmethod
    def _execution_guard(value: RuntimeExecution) -> tuple[Any, ...]:
        return (
            value.tenant_id,
            value.run_id,
            value.runtime_version_id,
            value.version,
            value.phase,
            value.current_owner_attempt_id,
            value.current_fencing_token,
        )


__all__ = ["CoordinatedRuntimeAggregate", "CoordinatedRuntimeAggregateLocker"]
