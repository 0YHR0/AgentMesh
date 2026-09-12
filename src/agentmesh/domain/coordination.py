from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase

AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


def normalize_agent_name(value: str) -> str:
    normalized = value.strip().lower()
    if not AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidTaskInput(
            "Preferred Agent name must be 3-63 lowercase letters, numbers, or hyphens"
        )
    return normalized


def validate_capability_key(value: str) -> str:
    normalized = value.strip().lower()
    if not CAPABILITY_PATTERN.fullmatch(normalized):
        raise InvalidTaskInput(
            "Capability key must be a namespaced value such as code.review.python"
        )
    return normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SubtaskStatus(str, Enum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL_SUBTASK_STATUSES = {
    SubtaskStatus.COMPLETED,
    SubtaskStatus.FAILED,
    SubtaskStatus.CANCELED,
}


class CoordinationRuntimeDrainStatus(str, Enum):
    DRAINING = "DRAINING"
    COMPLETE = "COMPLETE"


class CoordinationRuntimeDrainTarget(str, Enum):
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class CoordinationRuntimeBoundary(str, Enum):
    NOT_CROSSED_QUEUED = "NOT_CROSSED_QUEUED"
    NOT_CROSSED_NO_EXECUTION = "NOT_CROSSED_NO_EXECUTION"
    NOT_CROSSED_PREPARED = "NOT_CROSSED_PREPARED"
    CROSSED_ACTIVE = "CROSSED_ACTIVE"
    KNOWN_TERMINAL = "KNOWN_TERMINAL"
    RECONCILIATION_EVIDENCE = "RECONCILIATION_EVIDENCE"


def _is_utc_timestamp(value: datetime) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset() == timezone.utc.utcoffset(value)
    )


def normalize_coordination_reason(reason: str) -> str:
    """Normalize the bounded, non-control safe reason used by coordination holds."""
    normalized = reason.strip() if type(reason) is str else ""
    if (
        not normalized
        or len(normalized) > 4096
        or any(ord(character) < 32 for character in normalized)
    ):
        raise InvalidTaskInput("Coordination reconciliation requires a bounded safe reason")
    return normalized


def _transition_at(at: datetime, *, baseline: datetime | None = None) -> datetime:
    if type(at) is not datetime or at.tzinfo is None or at.utcoffset() is None:
        raise InvalidTaskInput("Coordination transition time must include a timezone")
    normalized = at.astimezone(timezone.utc)
    if baseline is not None and normalized < baseline:
        raise InvalidTaskTransition("Coordination transition clock cannot move backwards")
    return normalized


@dataclass(frozen=True)
class CoordinationRuntimeDrain:
    id: UUID
    tenant_id: str
    task_id: UUID
    triggering_run_id: UUID
    target: CoordinationRuntimeDrainTarget
    reason: str
    status: CoordinationRuntimeDrainStatus
    version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.id, self.task_id, self.triggering_run_id)
        ):
            raise InvalidTaskInput("Coordination Runtime drain identity is invalid")
        if (
            type(self.tenant_id) is not str
            or not self.tenant_id.strip()
            or self.tenant_id != self.tenant_id.strip()
            or len(self.tenant_id) > 128
            or type(self.target) is not CoordinationRuntimeDrainTarget
            or type(self.status) is not CoordinationRuntimeDrainStatus
            or type(self.reason) is not str
            or not self.reason.strip()
            or self.reason != self.reason.strip()
            or len(self.reason) > 4096
            or type(self.version) is not int
            or self.version <= 0
            or not _is_utc_timestamp(self.created_at)
            or not _is_utc_timestamp(self.updated_at)
            or self.updated_at < self.created_at
            or (
                self.completed_at is not None
                and not _is_utc_timestamp(self.completed_at)
            )
            or (
                self.status is CoordinationRuntimeDrainStatus.DRAINING
                and self.completed_at is not None
            )
            or (
                self.status is CoordinationRuntimeDrainStatus.COMPLETE
                and (
                    self.completed_at is None
                    or self.completed_at < self.created_at
                    or self.updated_at < self.completed_at
                )
            )
        ):
            raise InvalidTaskInput("Coordination Runtime drain projection is invalid")

    @classmethod
    def start(
        cls,
        *,
        drain_id: UUID,
        tenant_id: str,
        task_id: UUID,
        triggering_run_id: UUID,
        target: CoordinationRuntimeDrainTarget,
        reason: str,
        at: datetime,
    ) -> CoordinationRuntimeDrain:
        timestamp = _transition_at(at)
        if type(target) is not CoordinationRuntimeDrainTarget:
            raise InvalidTaskInput("Coordination Runtime drain target is invalid")
        return cls(
            id=drain_id,
            tenant_id=tenant_id,
            task_id=task_id,
            triggering_run_id=triggering_run_id,
            target=target,
            reason=normalize_coordination_reason(reason),
            status=CoordinationRuntimeDrainStatus.DRAINING,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            completed_at=None,
        )

    def retarget(
        self,
        *,
        target: CoordinationRuntimeDrainTarget,
        reason: str,
        at: datetime,
    ) -> CoordinationRuntimeDrain:
        if type(target) is not CoordinationRuntimeDrainTarget:
            raise InvalidTaskInput("Coordination Runtime drain target is invalid")
        normalized_reason = normalize_coordination_reason(reason)
        if self.status is CoordinationRuntimeDrainStatus.COMPLETE:
            return self
        if target is self.target:
            return self
        allowed = {
            CoordinationRuntimeDrainTarget.RUNNING: {
                CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                CoordinationRuntimeDrainTarget.FAILED,
                CoordinationRuntimeDrainTarget.CANCELED,
            },
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL: {
                CoordinationRuntimeDrainTarget.FAILED,
                CoordinationRuntimeDrainTarget.CANCELED,
            },
            CoordinationRuntimeDrainTarget.FAILED: set(),
            CoordinationRuntimeDrainTarget.CANCELED: set(),
        }
        if target not in allowed[self.target]:
            return self
        timestamp = _transition_at(at, baseline=self.updated_at)
        return replace(
            self,
            target=target,
            reason=normalized_reason,
            version=self.version + 1,
            updated_at=timestamp,
        )

    def complete(self, *, at: datetime) -> CoordinationRuntimeDrain:
        if self.status is CoordinationRuntimeDrainStatus.COMPLETE:
            return self
        timestamp = _transition_at(at, baseline=self.updated_at)
        return replace(
            self,
            status=CoordinationRuntimeDrainStatus.COMPLETE,
            version=self.version + 1,
            updated_at=timestamp,
            completed_at=timestamp,
        )

    @property
    def stopping(self) -> bool:
        return self.target in {
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        }


def classify_runtime_boundary(
    *, subtask: Subtask, run: Any, latest_attempt: Any | None, executions: Iterable[Any]
) -> CoordinationRuntimeBoundary:
    """Classify one coordinated managed Executor boundary without mutating inputs."""
    # Local imports avoid making the domain modules cyclic while still rejecting
    # look-alike status values from callers at runtime.
    from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskRun

    if type(subtask) is not Subtask or type(run) is not TaskRun:
        raise InvalidTaskTransition("Coordinated Runtime boundary identity is invalid")
    if latest_attempt is not None:
        from agentmesh.domain.tasks import TaskAttempt

        if type(latest_attempt) is not TaskAttempt:
            raise InvalidTaskTransition("Coordinated Runtime boundary Attempt is invalid")
    try:
        execution_values = tuple(executions)
    except TypeError as exc:
        raise InvalidTaskTransition("Coordinated Runtime executions are invalid") from exc
    if any(type(value) is not _runtime_execution_type() for value in execution_values):
        raise InvalidTaskTransition("Coordinated Runtime execution is invalid")
    if len({value.id for value in execution_values}) != len(execution_values):
        raise InvalidTaskTransition("Coordinated Runtime execution identity is duplicated")
    if (
        type(subtask.id) is not UUID
        or type(subtask.task_id) is not UUID
        or type(subtask.status) is not SubtaskStatus
        or type(run.id) is not UUID
        or type(run.task_id) is not UUID
        or type(run.role) is not RunRole
        or type(run.status) is not RunStatus
        or run.role is not RunRole.EXECUTOR
        or run.runtime_authority != "managed"
        or run.task_id != subtask.task_id
        or run.subtask_id != subtask.id
        or subtask.current_run_id != run.id
        or type(run.runtime_execution_intent_id) is not UUID
    ):
        raise InvalidTaskTransition("Coordinated Runtime boundary ownership is invalid")
    if latest_attempt is not None and latest_attempt.run_id != run.id:
        raise InvalidTaskTransition("Coordinated Runtime boundary Attempt ownership is invalid")
    if run.status is RunStatus.QUEUED:
        if (
            latest_attempt is not None
            or subtask.status is not SubtaskStatus.READY
            or run.runtime_execution_id is not None
            or execution_values
        ):
            raise InvalidTaskTransition("Queued coordinated Runtime boundary is inconsistent")
        return CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
    if latest_attempt is None:
        raise InvalidTaskTransition("Non-queued coordinated Runtime boundary lacks an Attempt")
    known_terminal = {
        RuntimeExecutionPhase.SUCCEEDED,
        RuntimeExecutionPhase.FAILED,
        RuntimeExecutionPhase.CANCELED,
        RuntimeExecutionPhase.TIMED_OUT,
    }
    parked = {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}
    active_or_unresolved = [
        value
        for value in execution_values
        if value.phase not in known_terminal
    ]
    if len(active_or_unresolved) > 1:
        raise InvalidTaskTransition("Multiple active or unresolved Runtime executions exist")
    if run.runtime_execution_id is None:
        if (
            run.status is not RunStatus.RUNNING
            or subtask.status is not SubtaskStatus.RUNNING
            or latest_attempt.status is not AttemptStatus.RUNNING
        ):
            raise InvalidTaskTransition("Reconciliation boundary lacks a Runtime execution")
        if execution_values:
            raise InvalidTaskTransition("Unbound coordinated Runtime executions are inconsistent")
        return CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
    bound = [value for value in execution_values if value.id == run.runtime_execution_id]
    if len(bound) != 1 or run.runtime_execution_intent_id != run.runtime_execution_id:
        raise InvalidTaskTransition("Coordinated Runtime execution binding is invalid")
    execution = bound[0]
    if execution.run_id != run.id or execution.runtime_version_id != run.runtime_version_id:
        raise InvalidTaskTransition("Coordinated Runtime execution Run identity is invalid")
    if active_or_unresolved and active_or_unresolved[0].id != execution.id:
        raise InvalidTaskTransition("Coordinated Runtime execution state is ambiguous")
    if execution.current_owner_attempt_id != latest_attempt.id or (
        execution.current_fencing_token != latest_attempt.fencing_token
    ):
        raise InvalidTaskTransition("Coordinated Runtime execution owner is invalid")
    if execution.phase in parked:
        if (
            run.status is not RunStatus.RECONCILIATION_REQUIRED
            or subtask.status is not SubtaskStatus.RECONCILIATION_REQUIRED
            or latest_attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
        ):
            raise InvalidTaskTransition("Parked Runtime boundary status is inconsistent")
        return CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
    if execution.phase in known_terminal:
        terminal_statuses = {
            RuntimeExecutionPhase.SUCCEEDED: (
                AttemptStatus.SUCCEEDED,
                RunStatus.SUCCEEDED,
                SubtaskStatus.COMPLETED,
            ),
            RuntimeExecutionPhase.FAILED: (
                AttemptStatus.FAILED,
                RunStatus.FAILED,
                SubtaskStatus.FAILED,
            ),
            RuntimeExecutionPhase.TIMED_OUT: (
                AttemptStatus.FAILED,
                RunStatus.FAILED,
                SubtaskStatus.FAILED,
            ),
        }
        if execution.phase is RuntimeExecutionPhase.CANCELED:
            valid_canceled = (
                (
                    AttemptStatus.CANCELED,
                    RunStatus.CANCELED,
                    SubtaskStatus.CANCELED,
                ),
                (
                    AttemptStatus.FAILED,
                    RunStatus.FAILED,
                    SubtaskStatus.FAILED,
                ),
            )
            if (latest_attempt.status, run.status, subtask.status) not in valid_canceled:
                raise InvalidTaskTransition("Canceled Runtime boundary status is inconsistent")
        elif (latest_attempt.status, run.status, subtask.status) != terminal_statuses[
            execution.phase
        ]:
            raise InvalidTaskTransition("Terminal Runtime boundary status is inconsistent")
        return CoordinationRuntimeBoundary.KNOWN_TERMINAL
    if (
        run.status is not RunStatus.RUNNING
        or subtask.status is not SubtaskStatus.RUNNING
        or latest_attempt.status is not AttemptStatus.RUNNING
    ):
        raise InvalidTaskTransition("Active coordinated Runtime boundary status is inconsistent")
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
    raise InvalidTaskTransition("Coordinated Runtime execution phase is invalid")


def _runtime_execution_type() -> type:
    from agentmesh.domain.runtime_execution import RuntimeExecution

    return RuntimeExecution


@dataclass(frozen=True)
class SubtaskSpec:
    key: str
    objective: str
    input: dict[str, Any]
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...]
    preferred_agent_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        key: str,
        objective: str,
        input: dict[str, Any] | None = None,
        required_capabilities: tuple[str, ...] | list[str] = ("general.task",),
        depends_on: tuple[str, ...] | list[str] = (),
        preferred_agent_id: str | None = None,
    ) -> SubtaskSpec:
        normalized_key = key.strip()
        normalized_objective = objective.strip()
        if not normalized_key or len(normalized_key) > 128:
            raise InvalidTaskInput("Subtask key must contain 1 to 128 characters")
        if not normalized_objective:
            raise InvalidTaskInput("Subtask objective must not be empty")
        capabilities = tuple(
            sorted({validate_capability_key(value) for value in required_capabilities})
        )
        if not capabilities:
            raise InvalidTaskInput("Subtask requires at least one capability")
        dependencies = tuple(dict.fromkeys(value.strip() for value in depends_on))
        if any(not value for value in dependencies):
            raise InvalidTaskInput("Subtask dependencies must not be empty")
        agent_id = (
            normalize_agent_name(preferred_agent_id) if preferred_agent_id is not None else None
        )
        return cls(
            key=normalized_key,
            objective=normalized_objective,
            input=dict(input or {}),
            required_capabilities=capabilities,
            depends_on=dependencies,
            preferred_agent_id=agent_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "objective": self.objective,
            "input": dict(self.input),
            "required_capabilities": list(self.required_capabilities),
            "depends_on": list(self.depends_on),
            "preferred_agent_id": self.preferred_agent_id,
        }


@dataclass(frozen=True)
class CoordinatedPlan:
    version: int
    digest: str
    max_concurrency: int
    specs: tuple[SubtaskSpec, ...]

    @classmethod
    def create(
        cls,
        specs: tuple[SubtaskSpec, ...],
        *,
        max_concurrency: int,
        version: int = 1,
    ) -> CoordinatedPlan:
        if version < 1:
            raise InvalidTaskInput("Coordinated plan version must be positive")
        if not 2 <= len(specs) <= 20:
            raise InvalidTaskInput("A coordinated plan requires 2 to 20 Subtasks")
        if not 1 <= max_concurrency <= 10:
            raise InvalidTaskInput("Coordinated max_concurrency must be between 1 and 10")
        by_key = {spec.key: spec for spec in specs}
        if len(by_key) != len(specs):
            raise InvalidTaskInput("Subtask keys must be unique")
        edge_count = sum(len(spec.depends_on) for spec in specs)
        if edge_count > 100:
            raise InvalidTaskInput("A coordinated plan supports at most 100 dependencies")
        for spec in specs:
            missing = set(spec.depends_on) - set(by_key)
            if missing:
                raise InvalidTaskInput(
                    f"Subtask {spec.key} references missing dependencies: "
                    f"{', '.join(sorted(missing))}"
                )
            if spec.key in spec.depends_on:
                raise InvalidTaskInput(f"Subtask {spec.key} cannot depend on itself")
        cls._require_acyclic(specs)
        canonical = json.dumps(
            {
                "version": version,
                "max_concurrency": max_concurrency,
                "subtasks": [spec.to_dict() for spec in specs],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            version=version,
            digest=f"sha256:{sha256(canonical.encode()).hexdigest()}",
            max_concurrency=max_concurrency,
            specs=specs,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CoordinatedPlan:
        raw_specs = value.get("subtasks")
        if not isinstance(raw_specs, list):
            raise InvalidTaskInput("Coordinated plan snapshot must contain Subtasks")
        return cls.create(
            tuple(
                SubtaskSpec.create(
                    key=str(spec["key"]),
                    objective=str(spec["objective"]),
                    input=dict(spec.get("input", {})),
                    required_capabilities=tuple(spec.get("required_capabilities", ())),
                    depends_on=tuple(spec.get("depends_on", ())),
                    preferred_agent_id=spec.get("preferred_agent_id"),
                )
                for spec in raw_specs
                if isinstance(spec, dict)
            ),
            max_concurrency=int(value["max_concurrency"]),
            version=int(value["version"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "digest": self.digest,
            "max_concurrency": self.max_concurrency,
            "subtasks": [spec.to_dict() for spec in self.specs],
        }

    @staticmethod
    def _require_acyclic(specs: tuple[SubtaskSpec, ...]) -> None:
        remaining = {spec.key: set(spec.depends_on) for spec in specs}
        ready = sorted(key for key, dependencies in remaining.items() if not dependencies)
        visited = 0
        while ready:
            key = ready.pop(0)
            visited += 1
            for successor, dependencies in remaining.items():
                if key not in dependencies:
                    continue
                dependencies.remove(key)
                if not dependencies:
                    ready.append(successor)
                    ready.sort()
        if visited != len(specs):
            raise InvalidTaskInput("Coordinated plan must be acyclic")

    def materialize(self, task_id: UUID) -> tuple[list[Subtask], list[SubtaskDependency]]:
        ids = {spec.key: uuid4() for spec in self.specs}
        subtasks = [
            Subtask.create(
                subtask_id=ids[spec.key],
                task_id=task_id,
                key=spec.key,
                objective=spec.objective,
                input=spec.input,
                required_capabilities=spec.required_capabilities,
                preferred_agent_id=spec.preferred_agent_id,
                initially_ready=not spec.depends_on,
            )
            for spec in self.specs
        ]
        dependencies = [
            SubtaskDependency(
                task_id=task_id,
                predecessor_id=ids[dependency],
                successor_id=ids[spec.key],
            )
            for spec in self.specs
            for dependency in spec.depends_on
        ]
        return subtasks, dependencies


@dataclass
class Subtask:
    id: UUID
    task_id: UUID
    key: str
    objective: str
    input: dict[str, Any]
    required_capabilities: tuple[str, ...]
    preferred_agent_id: str | None
    status: SubtaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        subtask_id: UUID,
        task_id: UUID,
        key: str,
        objective: str,
        input: dict[str, Any],
        required_capabilities: tuple[str, ...],
        preferred_agent_id: str | None,
        initially_ready: bool,
    ) -> Subtask:
        now = utc_now()
        return cls(
            id=subtask_id,
            task_id=task_id,
            key=key,
            objective=objective,
            input=dict(input),
            required_capabilities=tuple(required_capabilities),
            preferred_agent_id=preferred_agent_id,
            status=SubtaskStatus.READY if initially_ready else SubtaskStatus.BLOCKED,
            current_run_id=None,
            output=None,
            error=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def mark_ready(self, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        if self.status == SubtaskStatus.READY:
            return
        self._require_status(SubtaskStatus.BLOCKED, "mark ready")
        self.status = SubtaskStatus.READY
        self._touch(at=at)

    def queue(self, run_id: UUID, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        self._require_status(SubtaskStatus.READY, "queue")
        if self.current_run_id is not None:
            raise InvalidTaskTransition(f"Subtask {self.id} already has a Run")
        self.current_run_id = run_id
        self._touch(at=at)

    def start(self, run_id: UUID, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.READY, "start")
        self.status = SubtaskStatus.RUNNING
        self._touch(at=at)

    def require_runtime_reconciliation(
        self, run_id: UUID, reason: str, *, at: datetime | None = None
    ) -> None:
        normalized = normalize_coordination_reason(reason)
        if self.status is SubtaskStatus.RECONCILIATION_REQUIRED:
            self._require_current_run(run_id)
            if self.error == normalized:
                return
            raise InvalidTaskTransition(
                f"Subtask {self.id} already has a different reconciliation reason"
            )
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RUNNING, "require Runtime reconciliation")
        self.status = SubtaskStatus.RECONCILIATION_REQUIRED
        self.output = None
        self.error = normalized
        self._touch(at=at)

    def reconcile_runtime_succeeded(
        self, run_id: UUID, output: Mapping[str, Any], *, at: datetime | None = None
    ) -> None:
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RECONCILIATION_REQUIRED, "reconcile Runtime success")
        if not isinstance(output, Mapping):
            raise InvalidTaskInput("Successful Subtask reconciliation requires a mapping output")
        self.status = SubtaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch(at=at)

    def reconcile_runtime_failed(
        self, run_id: UUID, reason: str, *, at: datetime | None = None
    ) -> None:
        self._reconcile_runtime_terminal(run_id, SubtaskStatus.FAILED, reason, at=at)

    def reconcile_runtime_canceled(
        self, run_id: UUID, reason: str, *, at: datetime | None = None
    ) -> None:
        self._reconcile_runtime_terminal(run_id, SubtaskStatus.CANCELED, reason, at=at)

    def _reconcile_runtime_terminal(
        self,
        run_id: UUID,
        status: SubtaskStatus,
        reason: str,
        *,
        at: datetime | None,
    ) -> None:
        normalized = normalize_coordination_reason(reason)
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RECONCILIATION_REQUIRED, "reconcile Runtime outcome")
        if status not in {SubtaskStatus.FAILED, SubtaskStatus.CANCELED}:
            raise InvalidTaskTransition("Subtask reconciliation target is invalid")
        self.status = status
        self.output = None
        self.error = normalized
        self._touch(at=at)

    def release_never_dispatched_run(
        self, run_id: UUID, *, at: datetime | None = None
    ) -> None:
        self._validate_at(at)
        self._require_current_run(run_id)
        if self.status not in {SubtaskStatus.READY, SubtaskStatus.RUNNING}:
            raise InvalidTaskTransition(
                f"Cannot release Subtask {self.id} from status {self.status.value}"
            )
        self.status = SubtaskStatus.READY
        self.current_run_id = None
        self.output = None
        self.error = None
        self._touch(at=at)

    def complete(self, run_id: UUID, output: dict[str, Any], *, at: datetime | None = None) -> None:
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RUNNING, "complete")
        self.status = SubtaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch(at=at)

    def fail(self, run_id: UUID, error: str, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RUNNING, "fail")
        normalized = error.strip()
        if not normalized:
            raise InvalidTaskInput("Subtask failure must include an error summary")
        self.status = SubtaskStatus.FAILED
        self.output = None
        self.error = normalized
        self._touch(at=at)

    def cancel(self, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        if self.status is SubtaskStatus.RECONCILIATION_REQUIRED:
            raise InvalidTaskTransition(
                f"Cannot cancel Subtask {self.id} from status {self.status.value}"
            )
        if self.status in TERMINAL_SUBTASK_STATUSES:
            return
        self.status = SubtaskStatus.CANCELED
        self._touch(at=at)

    def reopen_after_budget(self, *, at: datetime | None = None) -> None:
        self._validate_at(at)
        self._require_status(SubtaskStatus.CANCELED, "reopen after budget")
        self.status = SubtaskStatus.BLOCKED
        self.current_run_id = None
        self.output = None
        self.error = None
        self._touch(at=at)

    def _require_status(self, expected: SubtaskStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} Subtask {self.id} from status {self.status.value}"
            )

    def _require_current_run(self, run_id: UUID) -> None:
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(f"Run {run_id} is not active for Subtask {self.id}")

    def _validate_at(self, at: datetime | None) -> None:
        if at is None:
            return
        if type(at) is not datetime or at.tzinfo is None or at.utcoffset() is None:
            raise InvalidTaskInput("Policy transition time must include a timezone")
        if at.astimezone(timezone.utc) < self.updated_at.astimezone(timezone.utc):
            raise InvalidTaskTransition("Policy clock cannot move backwards")

    def _touch(self, *, at: datetime | None = None) -> None:
        self.version += 1
        if at is None:
            self.updated_at = utc_now()
        elif type(at) is datetime and at.tzinfo is not None and at.utcoffset() is not None:
            self.updated_at = at.astimezone(timezone.utc)
        else:
            raise InvalidTaskInput("Policy transition time must include a timezone")


@dataclass(frozen=True)
class SubtaskDependency:
    task_id: UUID
    predecessor_id: UUID
    successor_id: UUID
