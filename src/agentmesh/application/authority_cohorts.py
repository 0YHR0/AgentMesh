"""Immutable Runtime authority cohort resolution for local Task Runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.runtime_execution import RuntimeTrustProfile, RuntimeVersionStatus
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk.builtin import (
    LANGGRAPH_V2_DESCRIPTOR,
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
)
from agentmesh.runtime_sdk.canonical import canonical_digest
from agentmesh.runtime_sdk.common import RuntimeContractError
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


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
        if self.runtime_authority == "managed":
            if self.runtime_version_id is None or self.comparison_mode != "off":
                raise InvalidTaskInput("Managed authority cohort is incomplete")
        elif self.comparison_mode == "off" and self.runtime_version_id is not None:
            raise InvalidTaskInput("Legacy authority cohort cannot carry a Runtime Version")
        elif self.comparison_mode == "deterministic_shadow" and self.runtime_version_id is None:
            raise InvalidTaskInput("Shadow authority cohort requires a Runtime Version")


class ContinuationKind(str, Enum):
    """Closed lineage vocabulary for a locally-created continuation Run."""

    REVIEWER = "REVIEWER"
    REVISION = "REVISION"
    REPLACEMENT = "REPLACEMENT"
    COORDINATED = "COORDINATED"


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
        task, prior_runs = self._lock_task_and_runs(uow, task)
        if prior_runs:
            raise InvalidTaskTransition("Initial Runtime admission requires a Task with no Runs")
        return self._initial_for_locked_task(
            uow,
            task,
            runtime_version_id=runtime_version_id,
            comparison_mode=comparison_mode,
        )

    def _initial_for_locked_task(
        self,
        uow: Any,
        task: Task,
        *,
        runtime_version_id: UUID | None,
        comparison_mode: str,
    ) -> AuthorityCohort:
        if comparison_mode not in {"off", "deterministic_shadow"}:
            raise InvalidTaskInput("Run comparison mode is invalid")
        if task.execution_mode is not TaskExecutionMode.DIRECT and runtime_version_id is not None:
            raise InvalidTaskInput("Runtime Version is only valid for DIRECT admission")
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
            self._require_inherited_runtime_version(uow, task, version.id, version=version)
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

    def resolve_continuation_cohort_in_uow(
        self, uow: Any, task: Task
    ) -> AuthorityCohort:
        """Lock a Task and all existing Runs, then resolve one immutable cohort."""
        locked_task, runs = self._lock_task_and_runs(uow, task)
        if locked_task.id != task.id or locked_task.tenant_id != task.tenant_id:
            raise RuntimeExecutionConflict("Task cohort identity changed during resolution")
        return self._cohort_from_runs(uow, locked_task, runs)

    def create_continuation_from_cohort_in_uow(
        self,
        uow: Any,
        task: Task,
        *,
        cohort: AuthorityCohort,
        agent_id: str,
        agent_version_id: UUID | None,
        agent_version_digest: str | None,
        role: RunRole,
        revision_number: int = 0,
        subtask_id: UUID | None = None,
        parent_run: TaskRun | None = None,
        kind: ContinuationKind | None = None,
    ) -> TaskRun:
        """Create from a caller-resolved cohort without re-reading admission policy."""
        locked_task = task
        locked_runs: list[TaskRun] = []
        if parent_run is not None:
            locked_task, locked_runs = self._lock_task_and_runs(uow, task)
        kind = self._infer_kind(parent_run, role, revision_number, kind)
        self._validate_parent(locked_task, locked_runs, parent_run, role, revision_number, kind)
        if cohort.runtime_authority == "managed":
            assert cohort.runtime_version_id is not None
            self._require_inherited_runtime_version(uow, locked_task, cohort.runtime_version_id)
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
        kind: ContinuationKind | None = None,
    ) -> TaskRun:
        """Create one fully-bound Run without persistence or messaging side effects."""
        cohort = self.resolve_continuation_cohort_in_uow(uow, task)
        return self.create_continuation_from_cohort_in_uow(
            uow,
            task,
            cohort=cohort,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            role=role,
            revision_number=revision_number,
            subtask_id=subtask_id,
            parent_run=parent_run,
            kind=kind,
        )

    def _cohort_from_runs(self, uow: Any, task: Task, runs: list[TaskRun]) -> AuthorityCohort:
        if not runs:
            return self._initial_for_locked_task(
                uow, task, runtime_version_id=None, comparison_mode="off"
            )
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
    def _lock_task_and_runs(uow: Any, task: Task) -> tuple[Task, list[TaskRun]]:
        tasks = getattr(uow, "tasks", None)
        persisted = tasks.get(task.id, for_update=True) if tasks is not None else task
        if persisted is None or persisted.id != task.id or persisted.tenant_id != task.tenant_id:
            raise RuntimeExecutionConflict("Task cohort identity is unavailable")
        runs = list(uow.runs.list_for_task(task.id))
        locked_runs: list[TaskRun] = []
        for candidate in runs:
            locked = uow.runs.get(candidate.id, for_update=True)
            if locked is None or locked.task_id != task.id:
                raise RuntimeExecutionConflict("Task Run cohort changed during resolution")
            locked_runs.append(locked)
        return persisted, locked_runs

    @staticmethod
    def _validate_parent(
        task: Task,
        locked_runs: list[TaskRun],
        parent_run: TaskRun | None,
        role: RunRole,
        revision_number: int,
        kind: ContinuationKind,
    ) -> None:
        if kind is ContinuationKind.COORDINATED:
            if parent_run is not None or role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}:
                raise InvalidTaskTransition("Coordinated continuation lineage is invalid")
            return
        if parent_run is None:
            raise InvalidTaskTransition("Continuation requires a persisted parent Run")
        persisted = next((run for run in locked_runs if run.id == parent_run.id), None)
        if persisted is None or not AuthorityCohortResolver._same_parent_identity(
            persisted, parent_run
        ):
            raise InvalidTaskTransition("Continuation parent is not the locked persisted Run")
        if kind is ContinuationKind.REVIEWER:
            if role is not RunRole.REVIEWER or parent_run.role is not RunRole.EXECUTOR:
                raise InvalidTaskTransition("Reviewer continuation lineage is invalid")
            if revision_number != parent_run.revision_number:
                raise InvalidTaskTransition("Reviewer continuation must keep its revision")
        elif kind is ContinuationKind.REVISION:
            if role is not RunRole.EXECUTOR or parent_run.role is not RunRole.REVIEWER:
                raise InvalidTaskTransition("Revision continuation lineage is invalid")
            if revision_number != parent_run.revision_number + 1:
                raise InvalidTaskTransition("Revision continuation must increment its revision")
        elif kind is ContinuationKind.REPLACEMENT:
            if role is not parent_run.role or revision_number != parent_run.revision_number:
                raise InvalidTaskTransition(
                    "Replacement continuation must preserve role and revision"
                )
        else:
            raise InvalidTaskInput("Unknown continuation kind")

    @staticmethod
    def _infer_kind(
        parent_run: TaskRun | None,
        role: RunRole,
        revision_number: int,
        kind: ContinuationKind | None,
    ) -> ContinuationKind:
        if kind is not None:
            return kind
        if parent_run is not None:
            if role is RunRole.REVIEWER and parent_run.role is RunRole.EXECUTOR:
                return ContinuationKind.REVIEWER
            if role is RunRole.EXECUTOR and parent_run.role is RunRole.REVIEWER:
                if revision_number == parent_run.revision_number + 1:
                    return ContinuationKind.REVISION
            if role is parent_run.role and revision_number == parent_run.revision_number:
                return ContinuationKind.REPLACEMENT
        if parent_run is None and role is RunRole.SUPERVISOR:
            return ContinuationKind.COORDINATED
        return ContinuationKind.REPLACEMENT

    @staticmethod
    def _same_parent_identity(persisted: TaskRun, candidate: TaskRun) -> bool:
        """Compare immutable lineage/binding fields, allowing the caller's status mutation."""
        return all(
            getattr(persisted, field) == getattr(candidate, field)
            for field in (
                "id",
                "task_id",
                "thread_id",
                "agent_id",
                "agent_version_id",
                "agent_version_digest",
                "role",
                "revision_number",
                "subtask_id",
                "runtime_version_id",
                "runtime_execution_id",
                "runtime_execution_intent_id",
                "runtime_authority",
                "comparison_mode",
            )
        )

    @staticmethod
    def _require_inherited_runtime_version(
        uow: Any, task: Task, version_id: UUID, *, version: Any | None = None
    ) -> None:
        if version is None:
            version = uow.runtimes.get_version(
                version_id, tenant_id=task.tenant_id, for_update=True
            )
        if version is None or version.status not in {
            RuntimeVersionStatus.PUBLISHED,
            RuntimeVersionStatus.DEPRECATED,
        }:
            raise RuntimeVersionNotFound("Pinned cohort Runtime Version is unavailable")
        descriptor = dict(version.descriptor)
        try:
            parsed = RuntimeDescriptor.from_dict(descriptor)
        except (RuntimeContractError, TypeError, ValueError, KeyError) as exc:
            raise RuntimeVersionNotFound("Pinned cohort Runtime Version is corrupt") from exc
        expected = RuntimeDescriptor.from_dict(LANGGRAPH_V2_DESCRIPTOR)
        expected_config_digest = canonical_digest(
            {
                "runtime_key": LANGGRAPH_V2_DESCRIPTOR["runtime_key"],
                "capabilities": LANGGRAPH_V2_DESCRIPTOR["capabilities"],
                "limits": LANGGRAPH_V2_DESCRIPTOR["limits"],
            }
        )
        expected_artifact_digest = canonical_digest(
            {"package": "agentmesh", "runtime": "agentmesh.langgraph", "release": "v2"}
        )
        if (
            version.id != builtin_langgraph_version_id("v2")
            or version.runtime_id != builtin_langgraph_runtime_id()
            or version.api_version != 1
            or version.adapter_kind != "python-in-process"
            or parsed.to_dict() != expected.to_dict()
            or version.configuration_digest != expected_config_digest
            or version.artifact_digest != expected_artifact_digest
            or version.trust_profile is not RuntimeTrustProfile.BUILT_IN
            or dict(version.compatibility) != {}
        ):
            raise RuntimeVersionNotFound("Pinned cohort Runtime Version is incompatible")


__all__ = ["AuthorityCohort", "AuthorityCohortResolver", "ContinuationKind"]
