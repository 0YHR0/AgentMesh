"""Immutable Runtime authority cohort resolution for local Task Runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.runtime_execution import RuntimeVersionStatus
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class AuthorityCohort:
    """The immutable authority facts inherited by every local continuation."""

    runtime_authority: str
    runtime_version_id: UUID | None
    comparison_mode: str

    def __post_init__(self) -> None:
        if self.runtime_authority not in {"legacy", "managed"}:
            raise InvalidTaskInput("Runtime authority cohort is invalid")
        if self.comparison_mode not in {"off", "deterministic_shadow"}:
            raise InvalidTaskInput("Runtime comparison cohort is invalid")
        if self.runtime_authority == "managed" and (
            self.runtime_version_id is None or self.comparison_mode != "off"
        ):
            raise InvalidTaskInput("Managed authority cohort is incomplete")


class AuthorityCohortResolver:
    """Resolve first admission and inherit an already-persisted local cohort."""

    def __init__(
        self,
        *,
        feature_gates: FeatureGateSet,
        runtime_registry_service: Any | None = None,
    ) -> None:
        self._feature_gates = feature_gates
        self._runtime_registry_service = runtime_registry_service

    def initial_admission_in_uow(
        self,
        uow: Any,
        task: Task,
        *,
        runtime_version_id: UUID | None = None,
        comparison_mode: str = "off",
    ) -> AuthorityCohort:
        """Select a cohort for a Task with no local Run yet.

        REVIEWED/COORDINATED managed admission remains intentionally disabled;
        their current gate-off behavior is legacy and deterministic shadow is
        rejected for those modes.
        """
        if comparison_mode not in {"off", "deterministic_shadow"}:
            raise InvalidTaskInput("Run comparison mode is invalid")
        if task.execution_mode is not TaskExecutionMode.DIRECT:
            if comparison_mode != "off":
                raise InvalidTaskInput(
                    "Deterministic Runtime comparison is only available for DIRECT Runs"
                )
            return AuthorityCohort("legacy", None, "off")
        if comparison_mode == "deterministic_shadow":
            self._feature_gates.require(Feature.MANAGED_RUNTIME_WORKER)
            self._feature_gates.require(Feature.DUAL_RECORD_RUNTIME)
            if runtime_version_id is None:
                raise InvalidTaskInput("Deterministic Runtime Version is required")
            return AuthorityCohort("legacy", runtime_version_id, comparison_mode)
        if self._feature_gates.is_enabled(Feature.MANAGED_RUNTIME_DIRECT_CUTOVER):
            if self._runtime_registry_service is None:
                raise InvalidTaskInput("Managed Runtime cohort resolver is unavailable")
            version = self._runtime_registry_service.require_builtin_langgraph_v2_in_uow(uow)
            return AuthorityCohort("managed", version.id, "off")
        return AuthorityCohort("legacy", None, "off")

    # Short alias for callers that use the design terminology.
    initial = initial_admission_in_uow

    def create_initial_in_uow(
        self,
        uow: Any,
        task: Task,
        *,
        agent_id: str,
        agent_version_id: UUID | None,
        agent_version_digest: str | None,
        role: RunRole = RunRole.EXECUTOR,
        revision_number: int = 0,
        runtime_version_id: UUID | None = None,
        comparison_mode: str = "off",
    ) -> TaskRun:
        cohort = self.initial_admission_in_uow(
            uow,
            task,
            runtime_version_id=runtime_version_id,
            comparison_mode=comparison_mode,
        )
        return TaskRun.request(
            task.id,
            agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            role=role,
            revision_number=revision_number,
            runtime_version_id=cohort.runtime_version_id,
            comparison_mode=cohort.comparison_mode,
            runtime_authority=cohort.runtime_authority,
        )

    def create_continuation_in_uow(
        self,
        uow: Any,
        task: Task,
        *,
        agent_id: str,
        agent_version_id: UUID | None,
        agent_version_digest: str | None,
        role: RunRole,
        revision_number: int = 0,
        subtask_id: UUID | None = None,
        parent_run: TaskRun | None = None,
    ) -> TaskRun:
        """Create one fully-bound Run without persistence or messaging side effects."""
        runs = list(uow.runs.list_for_task(task.id))
        locked_runs: list[TaskRun] = []
        for candidate in runs:
            locked = uow.runs.get(candidate.id, for_update=True)
            if locked is None or locked.task_id != task.id:
                raise RuntimeExecutionConflict("Task Run cohort changed during resolution")
            locked_runs.append(locked)
        cohort = self._cohort_from_runs(uow, task, locked_runs)
        self._validate_parent(task, parent_run, role, revision_number)
        if cohort.runtime_authority == "managed":
            assert cohort.runtime_version_id is not None
            self._require_inherited_runtime_version(uow, task, cohort.runtime_version_id)
        elif cohort.comparison_mode == "off" and (cohort.runtime_version_id is not None):
            raise RuntimeExecutionConflict("Legacy cohort contains managed Runtime metadata")
        return TaskRun.request(
            task.id,
            agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            role=role,
            revision_number=revision_number,
            subtask_id=subtask_id,
            runtime_version_id=cohort.runtime_version_id,
            comparison_mode=cohort.comparison_mode,
            runtime_authority=cohort.runtime_authority,
        )

    def _cohort_from_runs(self, uow: Any, task: Task, runs: list[TaskRun]) -> AuthorityCohort:
        if not runs:
            return self.initial_admission_in_uow(uow, task)
        authorities = {run.runtime_authority for run in runs}
        if len(authorities) != 1 or authorities - {"legacy", "managed"}:
            raise RuntimeExecutionConflict("Task contains mixed Runtime authorities")
        authority = next(iter(authorities))
        versions = {run.runtime_version_id for run in runs}
        comparisons = {run.comparison_mode for run in runs}
        if len(comparisons) != 1:
            raise RuntimeExecutionConflict("Task contains mixed Runtime comparison cohorts")
        comparison = next(iter(comparisons))
        if authority == "managed":
            if len(versions) != 1 or None in versions or comparison != "off":
                raise RuntimeExecutionConflict("Task contains mixed managed Runtime Versions")
            version_id = next(iter(versions))
            assert version_id is not None
            self._require_inherited_runtime_version(uow, task, version_id)
            return AuthorityCohort("managed", version_id, "off")
        if comparison == "off" and any(
            run.runtime_execution_id is not None or run.runtime_execution_intent_id is not None
            for run in runs
        ):
            raise RuntimeExecutionConflict("Legacy cohort contains managed Runtime metadata")
        if comparison == "deterministic_shadow":
            if len(versions) != 1 or None in versions:
                raise RuntimeExecutionConflict("Shadow cohort contains mixed Runtime Versions")
            return AuthorityCohort("legacy", next(iter(versions)), comparison)
        if any(version is not None for version in versions):
            raise RuntimeExecutionConflict("Legacy cohort contains a Runtime Version")
        return AuthorityCohort("legacy", None, "off")

    @staticmethod
    def _validate_parent(
        task: Task,
        parent_run: TaskRun | None,
        role: RunRole,
        revision_number: int,
    ) -> None:
        if parent_run is None:
            return
        if parent_run.task_id != task.id:
            raise InvalidTaskTransition("Continuation parent belongs to another Task")
        if role is RunRole.REVIEWER and parent_run.role is not RunRole.EXECUTOR:
            raise InvalidTaskTransition("Reviewer continuation requires an executor parent")
        if (
            role is RunRole.EXECUTOR
            and revision_number > 0
            and parent_run.role is not RunRole.REVIEWER
        ):
            raise InvalidTaskTransition("Revision continuation requires a reviewer parent")

    @staticmethod
    def _require_inherited_runtime_version(uow: Any, task: Task, version_id: UUID) -> None:
        version = uow.runtimes.get_version(version_id, tenant_id=task.tenant_id, for_update=True)
        if version is None or version.status not in {
            RuntimeVersionStatus.PUBLISHED,
            RuntimeVersionStatus.DEPRECATED,
        }:
            raise RuntimeVersionNotFound("Pinned cohort Runtime Version is unavailable")
        descriptor = dict(version.descriptor)
        if not descriptor.get("runtime_key") or version.api_version != 1:
            raise RuntimeVersionNotFound("Pinned cohort Runtime Version is incompatible")


__all__ = ["AuthorityCohort", "AuthorityCohortResolver"]
