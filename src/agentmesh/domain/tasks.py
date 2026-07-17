from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition

if TYPE_CHECKING:
    from agentmesh.domain.handoffs import Handoff


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AttemptStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


class TaskExecutionMode(str, Enum):
    DIRECT = "DIRECT"
    REVIEWED = "REVIEWED"
    COORDINATED = "COORDINATED"


class RunRole(str, Enum):
    EXECUTOR = "EXECUTOR"
    REVIEWER = "REVIEWER"
    SUPERVISOR = "SUPERVISOR"


class AcceptanceCriterionKind(str, Enum):
    OUTPUT_PATH_EXISTS = "OUTPUT_PATH_EXISTS"
    OUTPUT_PATH_EQUALS = "OUTPUT_PATH_EQUALS"


@dataclass(frozen=True)
class AcceptanceCriterion:
    key: str
    description: str
    kind: AcceptanceCriterionKind
    path: tuple[str, ...]
    expected: Any = None
    required: bool = True

    @classmethod
    def create(
        cls,
        *,
        key: str,
        description: str,
        kind: AcceptanceCriterionKind,
        path: list[str] | tuple[str, ...],
        expected: Any = None,
        required: bool = True,
    ) -> AcceptanceCriterion:
        normalized_key = key.strip()
        normalized_description = description.strip()
        normalized_path = tuple(part.strip() for part in path)
        if not normalized_key or not normalized_description:
            raise InvalidTaskInput("Acceptance criterion key and description must not be empty")
        if not normalized_path or any(not part for part in normalized_path):
            raise InvalidTaskInput("Acceptance criterion path must contain non-empty segments")
        return cls(
            key=normalized_key,
            description=normalized_description,
            kind=kind,
            path=normalized_path,
            expected=expected,
            required=required,
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "key": self.key,
            "description": self.description,
            "kind": self.kind.value,
            "path": list(self.path),
            "required": self.required,
        }
        if self.kind == AcceptanceCriterionKind.OUTPUT_PATH_EQUALS:
            value["expected"] = self.expected
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AcceptanceCriterion:
        return cls.create(
            key=str(value["key"]),
            description=str(value["description"]),
            kind=AcceptanceCriterionKind(str(value["kind"])),
            path=list(value["path"]),
            expected=value.get("expected"),
            required=bool(value.get("required", True)),
        )


@dataclass(frozen=True)
class ReviewDecision:
    accepted: bool
    score_basis_points: int
    criteria: tuple[dict[str, Any], ...]
    feedback: tuple[str, ...]

    @classmethod
    def from_output(
        cls,
        output: dict[str, Any],
        expected_criteria: tuple[AcceptanceCriterion, ...],
    ) -> ReviewDecision:
        raw_results = output.get("criteria")
        if not isinstance(raw_results, list):
            raise InvalidTaskInput("Reviewer output must contain a criteria list")
        results_by_key: dict[str, dict[str, Any]] = {}
        for raw in raw_results:
            if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
                raise InvalidTaskInput("Each reviewer criterion result must contain a key")
            key = raw["key"]
            if key in results_by_key or not isinstance(raw.get("passed"), bool):
                raise InvalidTaskInput("Reviewer criterion keys must be unique with boolean passed")
            results_by_key[key] = {
                "key": key,
                "passed": raw["passed"],
                "reason": str(raw.get("reason", "")).strip(),
            }
        expected_keys = {criterion.key for criterion in expected_criteria}
        if set(results_by_key) != expected_keys:
            raise InvalidTaskInput("Reviewer output criteria do not match the task contract")
        ordered = tuple(results_by_key[criterion.key] for criterion in expected_criteria)
        required_passed = all(
            results_by_key[criterion.key]["passed"]
            for criterion in expected_criteria
            if criterion.required
        )
        passed_count = sum(1 for result in ordered if result["passed"])
        score = (passed_count * 10_000) // len(ordered)
        raw_feedback = output.get("feedback", [])
        if not isinstance(raw_feedback, list):
            raise InvalidTaskInput("Reviewer feedback must be a list")
        feedback = tuple(str(item).strip() for item in raw_feedback if str(item).strip())
        return cls(required_passed, score, ordered, feedback)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "score_basis_points": self.score_basis_points,
            "criteria": [dict(result) for result in self.criteria],
            "feedback": list(self.feedback),
        }


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}
TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
}


@dataclass
class Task:
    id: UUID
    tenant_id: str
    objective: str
    input: dict[str, Any]
    status: TaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    execution_mode: TaskExecutionMode
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    max_revisions: int
    revision_count: int
    review_deadline: datetime | None
    candidate_output: dict[str, Any] | None
    latest_review: dict[str, Any] | None
    plan_version: int | None
    plan_digest: str | None
    max_concurrency: int
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        objective: str,
        input: dict[str, Any] | None = None,
        execution_mode: TaskExecutionMode = TaskExecutionMode.DIRECT,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] = (),
        max_revisions: int = 0,
        review_deadline: datetime | None = None,
        plan_version: int | None = None,
        plan_digest: str | None = None,
        max_concurrency: int = 1,
    ) -> Task:
        normalized_tenant_id = tenant_id.strip()
        normalized_objective = objective.strip()
        if not normalized_tenant_id:
            raise InvalidTaskInput("Task tenant ID must not be empty")
        if not normalized_objective:
            raise InvalidTaskInput("Task objective must not be empty")
        criteria = tuple(acceptance_criteria)
        if execution_mode == TaskExecutionMode.REVIEWED:
            if not criteria:
                raise InvalidTaskInput("Reviewed tasks require acceptance criteria")
            if len(criteria) > 20:
                raise InvalidTaskInput("Reviewed tasks support at most 20 acceptance criteria")
            if len({criterion.key for criterion in criteria}) != len(criteria):
                raise InvalidTaskInput("Acceptance criterion keys must be unique")
            if not any(criterion.required for criterion in criteria):
                raise InvalidTaskInput("Reviewed tasks require at least one required criterion")
            if max_revisions < 0:
                raise InvalidTaskInput("Maximum revisions must not be negative")
            if plan_version is not None or plan_digest is not None or max_concurrency != 1:
                raise InvalidTaskInput("A coordinated plan is not valid for reviewed tasks")
        elif execution_mode == TaskExecutionMode.COORDINATED:
            if criteria or max_revisions or review_deadline is not None:
                raise InvalidTaskInput("Review policy is not supported by coordinated tasks yet")
            if plan_version != 1 or not (plan_digest or "").startswith("sha256:"):
                raise InvalidTaskInput("Coordinated tasks require an immutable plan snapshot")
            if not 1 <= max_concurrency <= 10:
                raise InvalidTaskInput("Coordinated max_concurrency must be between 1 and 10")
        elif criteria or max_revisions or review_deadline is not None:
            raise InvalidTaskInput("Review policy is only valid for reviewed tasks")
        elif plan_version is not None or plan_digest is not None or max_concurrency != 1:
            raise InvalidTaskInput("Plan policy is only valid for coordinated tasks")
        if review_deadline is not None:
            if review_deadline.utcoffset() is None:
                raise InvalidTaskInput("Review deadline must include a timezone")
            if review_deadline <= utc_now():
                raise InvalidTaskInput("Review deadline must be in the future")

        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=normalized_tenant_id,
            objective=normalized_objective,
            input=dict(input or {}),
            status=TaskStatus.CREATED,
            current_run_id=None,
            output=None,
            error=None,
            execution_mode=execution_mode,
            acceptance_criteria=criteria,
            max_revisions=max_revisions,
            revision_count=0,
            review_deadline=review_deadline,
            candidate_output=None,
            latest_review=None,
            plan_version=plan_version,
            plan_digest=plan_digest,
            max_concurrency=max_concurrency,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def queue(self, run_id: UUID) -> None:
        self._require_status(TaskStatus.CREATED, "queue")
        self.status = TaskStatus.READY
        self.current_run_id = run_id
        self.output = None
        self.error = None
        self._touch()

    def start(self, run_id: UUID) -> None:
        self._require_active_run(run_id, "start", expected=TaskStatus.READY)
        self.status = TaskStatus.RUNNING
        self._touch()

    def start_coordination(self) -> None:
        self._require_status(TaskStatus.CREATED, "start coordination")
        if self.execution_mode != TaskExecutionMode.COORDINATED:
            raise InvalidTaskTransition("Only coordinated tasks can start a Subtask plan")
        self.status = TaskStatus.RUNNING
        self.current_run_id = None
        self.output = None
        self.error = None
        self._touch()

    def queue_supervisor(self, run_id: UUID) -> None:
        self._require_status(TaskStatus.RUNNING, "queue supervisor")
        if self.execution_mode != TaskExecutionMode.COORDINATED:
            raise InvalidTaskTransition("Only coordinated tasks can queue a Supervisor")
        self.current_run_id = run_id
        self._touch()

    def fail_coordination(self, error: str) -> None:
        self._require_status(TaskStatus.RUNNING, "fail coordination")
        normalized = error.strip()
        if not normalized:
            raise InvalidTaskInput("Coordinated Task failure must include an error summary")
        self.status = TaskStatus.FAILED
        self.output = None
        self.error = normalized
        self._touch()

    def start_review(self, run_id: UUID) -> None:
        self._require_active_run(run_id, "start review", expected=TaskStatus.REVIEWING)

    def complete(self, run_id: UUID, output: dict[str, Any]) -> None:
        self._require_active_run(run_id, "complete", expected=TaskStatus.RUNNING)
        self.status = TaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch()

    def queue_review(self, run_id: UUID, output: dict[str, Any], reviewer_run_id: UUID) -> None:
        self._require_active_run(run_id, "queue review", expected=TaskStatus.RUNNING)
        if self.execution_mode != TaskExecutionMode.REVIEWED:
            raise InvalidTaskTransition("Direct tasks cannot queue a review")
        self.status = TaskStatus.REVIEWING
        self.current_run_id = reviewer_run_id
        self.candidate_output = dict(output)
        self.error = None
        self._touch()

    def apply_review(
        self,
        reviewer_run_id: UUID,
        decision: ReviewDecision,
        revision_run_id: UUID | None,
        evaluated_at: datetime | None = None,
    ) -> None:
        self._require_active_run(reviewer_run_id, "apply review", expected=TaskStatus.REVIEWING)
        if self.candidate_output is None:
            raise InvalidTaskTransition("Reviewed task has no candidate output")
        self.latest_review = decision.to_dict()
        if decision.accepted:
            self.status = TaskStatus.COMPLETED
            self.output = dict(self.candidate_output)
            self.error = None
        elif (
            self.review_deadline is not None
            and (evaluated_at or utc_now()) >= self.review_deadline
        ):
            self.status = TaskStatus.WAITING_APPROVAL
            self.error = "review_deadline_exceeded"
        elif self.revision_count >= self.max_revisions:
            self.status = TaskStatus.WAITING_APPROVAL
            self.error = "review_revision_limit_reached"
        elif revision_run_id is None:
            raise InvalidTaskTransition("A failed review requires a revision run")
        else:
            self.revision_count += 1
            self.status = TaskStatus.READY
            self.current_run_id = revision_run_id
            self.error = None
        self._touch()

    def request_pause(self, run_id: UUID) -> None:
        if self.status in {TaskStatus.PAUSE_REQUESTED, TaskStatus.PAUSED}:
            self._require_current_run(run_id)
            return
        self._require_current_run(run_id)
        if self.status == TaskStatus.READY:
            self.status = TaskStatus.PAUSED
        elif self.status == TaskStatus.RUNNING:
            self.status = TaskStatus.PAUSE_REQUESTED
        else:
            raise InvalidTaskTransition(
                f"Cannot pause task {self.id} from status {self.status.value}"
            )
        self._touch()

    def mark_paused(self, run_id: UUID) -> None:
        self._require_active_run(
            run_id,
            "mark paused",
            expected=TaskStatus.PAUSE_REQUESTED,
        )
        self.status = TaskStatus.PAUSED
        self._touch()

    def resume(self, run_id: UUID) -> None:
        self._require_active_run(run_id, "resume", expected=TaskStatus.PAUSED)
        self.status = TaskStatus.READY
        self._touch()

    def fail(self, run_id: UUID, error: str) -> None:
        self._require_current_run(run_id)
        if self.status not in {
            TaskStatus.RUNNING,
            TaskStatus.REVIEWING,
            TaskStatus.PAUSE_REQUESTED,
        }:
            raise InvalidTaskTransition(
                f"Cannot fail task {self.id} from status {self.status.value}"
            )
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Task failure must include an error summary")
        self.status = TaskStatus.FAILED
        self.output = None
        self.error = normalized_error
        self._touch()

    def cancel(self) -> None:
        if self.status in TERMINAL_TASK_STATUSES:
            raise InvalidTaskTransition(
                f"Cannot cancel task {self.id} from terminal status {self.status.value}"
            )
        self.status = TaskStatus.CANCELED
        self._touch()

    def _require_status(self, expected: TaskStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} task {self.id} from status {self.status.value}"
            )

    def _require_active_run(
        self,
        run_id: UUID,
        action: str,
        *,
        expected: TaskStatus,
    ) -> None:
        self._require_status(expected, action)
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(f"Run {run_id} is not the active run for task {self.id}")

    def _require_current_run(self, run_id: UUID) -> None:
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(f"Run {run_id} is not the active run for task {self.id}")

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class TaskRun:
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
    output: dict[str, Any] | None
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    pause_requested_at: datetime | None
    paused_at: datetime | None
    resumed_at: datetime | None
    paused_from_status: RunStatus | None

    @classmethod
    def request(
        cls,
        task_id: UUID,
        agent_id: str,
        *,
        agent_version_id: UUID | None = None,
        agent_version_digest: str | None = None,
        role: RunRole = RunRole.EXECUTOR,
        revision_number: int = 0,
        subtask_id: UUID | None = None,
    ) -> TaskRun:
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise InvalidTaskInput("Agent ID must not be empty")
        if revision_number < 0:
            raise InvalidTaskInput("Run revision number must not be negative")
        run_id = uuid4()
        return cls(
            id=run_id,
            task_id=task_id,
            thread_id=str(run_id),
            agent_id=normalized_agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            role=role,
            revision_number=revision_number,
            subtask_id=subtask_id,
            status=RunStatus.QUEUED,
            output=None,
            error=None,
            queued_at=utc_now(),
            started_at=None,
            completed_at=None,
            pause_requested_at=None,
            paused_at=None,
            resumed_at=None,
            paused_from_status=None,
        )

    def start(self) -> None:
        self._require_status(RunStatus.QUEUED, "start")
        self.status = RunStatus.RUNNING
        if self.started_at is None:
            self.started_at = utc_now()

    def request_pause(self) -> None:
        if self.status in {RunStatus.PAUSE_REQUESTED, RunStatus.PAUSED}:
            return
        now = utc_now()
        if self.status == RunStatus.QUEUED:
            self.paused_from_status = self.status
            self.status = RunStatus.PAUSED
            self.pause_requested_at = now
            self.paused_at = now
        elif self.status == RunStatus.RUNNING:
            self.paused_from_status = self.status
            self.status = RunStatus.PAUSE_REQUESTED
            self.pause_requested_at = now
        else:
            raise InvalidTaskTransition(
                f"Cannot pause run {self.id} from status {self.status.value}"
            )

    def mark_paused(self) -> None:
        self._require_status(RunStatus.PAUSE_REQUESTED, "mark paused")
        self.status = RunStatus.PAUSED
        self.paused_at = utc_now()

    def resume(self) -> None:
        self._require_status(RunStatus.PAUSED, "resume")
        now = utc_now()
        self.status = RunStatus.QUEUED
        self.queued_at = now
        self.resumed_at = now

    def succeed(self, output: dict[str, Any]) -> None:
        self._require_status(RunStatus.RUNNING, "succeed")
        self.status = RunStatus.SUCCEEDED
        self.output = dict(output)
        self.error = None
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        if self.status not in {RunStatus.RUNNING, RunStatus.PAUSE_REQUESTED}:
            raise InvalidTaskTransition(
                f"Cannot fail run {self.id} from status {self.status.value}"
            )
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Run failure must include an error summary")
        self.status = RunStatus.FAILED
        self.output = None
        self.error = normalized_error
        self.completed_at = utc_now()

    def cancel(self) -> None:
        if self.status not in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSE_REQUESTED,
            RunStatus.PAUSED,
        }:
            raise InvalidTaskTransition(
                f"Cannot cancel run {self.id} from status {self.status.value}"
            )
        self.status = RunStatus.CANCELED
        self.completed_at = utc_now()

    def _require_status(self, expected: RunStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} run {self.id} from status {self.status.value}"
            )


@dataclass
class TaskAttempt:
    id: UUID
    run_id: UUID
    trace_id: str
    worker_id: str
    lease_token: UUID
    fencing_token: int
    status: AttemptStatus
    lease_expires_at: datetime
    heartbeat_at: datetime
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    @classmethod
    def lease(
        cls,
        *,
        run_id: UUID,
        worker_id: str,
        fencing_token: int,
        lease_expires_at: datetime,
    ) -> TaskAttempt:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id:
            raise InvalidTaskInput("Worker ID must not be empty")
        now = utc_now()
        attempt_id = uuid4()
        return cls(
            id=attempt_id,
            run_id=run_id,
            trace_id=attempt_id.hex,
            worker_id=normalized_worker_id,
            lease_token=uuid4(),
            fencing_token=fencing_token,
            status=AttemptStatus.RUNNING,
            lease_expires_at=lease_expires_at,
            heartbeat_at=now,
            started_at=now,
            completed_at=None,
            error=None,
        )

    def succeed(self) -> None:
        self._require_running("succeed")
        self.status = AttemptStatus.SUCCEEDED
        self.completed_at = utc_now()

    def pause(self) -> None:
        self._require_running("pause")
        self.status = AttemptStatus.PAUSED
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        self._require_running("fail")
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Attempt failure must include an error summary")
        self.status = AttemptStatus.FAILED
        self.error = normalized_error
        self.completed_at = utc_now()

    def cancel(self) -> None:
        self._require_running("cancel")
        self.status = AttemptStatus.CANCELED
        self.completed_at = utc_now()

    def expire(self) -> None:
        self._require_running("expire")
        self.status = AttemptStatus.LEASE_EXPIRED
        self.completed_at = utc_now()

    def renew(
        self,
        *,
        worker_id: str,
        lease_token: UUID,
        lease_expires_at: datetime,
        heartbeat_at: datetime | None = None,
    ) -> None:
        self._require_running("renew")
        if self.worker_id != worker_id or self.lease_token != lease_token:
            raise InvalidTaskTransition(
                f"Worker {worker_id} does not own attempt {self.id}"
            )
        now = heartbeat_at or utc_now()
        if lease_expires_at <= now:
            raise InvalidTaskInput("Attempt lease renewal must extend into the future")
        self.lease_expires_at = lease_expires_at
        self.heartbeat_at = now

    def _require_running(self, action: str) -> None:
        if self.status != AttemptStatus.RUNNING:
            raise InvalidTaskTransition(
                f"Cannot {action} attempt {self.id} from status {self.status.value}"
            )


@dataclass(frozen=True)
class TaskAggregate:
    task: Task
    runs: list[TaskRun] = field(default_factory=list)
    attempts: list[TaskAttempt] = field(default_factory=list)
    subtasks: list[Subtask] = field(default_factory=list)
    dependencies: list[SubtaskDependency] = field(default_factory=list)
    handoffs: list[Handoff] = field(default_factory=list)
