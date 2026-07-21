from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.ports import UnitOfWorkFactory, WorkflowRunner, WorkflowWorkItem
from agentmesh.application.quota_services import QuotaAdmissionRejected, QuotaController
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, Subtask, SubtaskDependency, SubtaskStatus
from agentmesh.domain.errors import (
    AgentUnavailable,
    IdempotencyConflict,
    InvalidMessage,
    InvalidTaskInput,
    InvalidTaskTransition,
    InvalidToolRequest,
    RunLeaseUnavailable,
    TaskExecutionFailed,
    TaskNotFound,
)
from agentmesh.domain.handoffs import Handoff
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    IdempotencyRecord,
    InboxMessage,
    MessageEnvelope,
)
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.planning import GoalContract
from agentmesh.domain.registry import AgentVersion, AgentVersionStatus, normalize_agent_name
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AttemptStatus,
    ReviewDecision,
    RunRole,
    RunStatus,
    Task,
    TaskAggregate,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)
from agentmesh.domain.tools import (
    WORKSPACE_READ_TOOL_KEY,
    ToolAuthorizationDraft,
    ToolCallRequest,
    ToolExecutionAuthorization,
)
from agentmesh.features import Feature, FeatureGateSet

logger = logging.getLogger(__name__)


class TaskApplicationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        agent_id: str,
        tenant_id: str,
        reviewer_agent_id: str = "demo-reviewer",
        max_review_revisions: int = 3,
        supervisor_agent_id: str = "demo-supervisor",
        max_coordinated_concurrency: int = 4,
        feature_gates: FeatureGateSet | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._reviewer_agent_id = reviewer_agent_id
        self._max_review_revisions = max_review_revisions
        self._max_coordinated_concurrency = max_coordinated_concurrency
        self._coordinated_scheduler = CoordinatedScheduler(supervisor_agent_id=supervisor_agent_id)
        self._feature_gates = feature_gates or FeatureGateSet.from_config("minimal")

    def create_task(
        self,
        objective: str,
        input: dict[str, Any] | None = None,
        execution_mode: TaskExecutionMode = TaskExecutionMode.DIRECT,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] = (),
        max_revisions: int = 0,
        review_deadline: datetime | None = None,
        coordinated_plan: CoordinatedPlan | None = None,
        budget: TaskBudget | None = None,
        tool_authorization: ToolAuthorizationDraft | None = None,
        project_id: str = "default",
        goal_constraints: tuple[str, ...] = (),
        goal_success_criteria: tuple[str, ...] = (),
    ) -> TaskAggregate:
        normalized_input = dict(input or {})
        tool_request = ToolCallRequest.from_task_input(normalized_input)
        if tool_request is not None:
            if execution_mode == TaskExecutionMode.FEDERATED:
                raise InvalidToolRequest("Federated Tasks cannot request a local MCP Tool")
            self._feature_gates.require(Feature.MCP_READ_TOOLS)
            if (
                not self._feature_gates.is_enabled(Feature.GOVERNED_MCP)
                and tool_request.tool_key != WORKSPACE_READ_TOOL_KEY
            ):
                raise InvalidToolRequest(
                    f"Tool '{tool_request.tool_key}' is not in the current allowlist"
                )
        if execution_mode == TaskExecutionMode.REVIEWED:
            self._feature_gates.require(Feature.REVIEWED_EXECUTION)
            if max_revisions > self._max_review_revisions:
                raise InvalidTaskInput(
                    f"Maximum revisions exceeds the platform limit of {self._max_review_revisions}"
                )
        elif execution_mode == TaskExecutionMode.COORDINATED:
            self._feature_gates.require(Feature.COORDINATED_EXECUTION)
            if coordinated_plan is None:
                raise InvalidTaskInput("Coordinated tasks require a Subtask plan")
            if coordinated_plan.max_concurrency > self._max_coordinated_concurrency:
                raise InvalidTaskInput(
                    "Coordinated max_concurrency exceeds the platform limit of "
                    f"{self._max_coordinated_concurrency}"
                )
        elif execution_mode == TaskExecutionMode.FEDERATED:
            self._feature_gates.require(Feature.A2A_DELEGATION)
        if execution_mode != TaskExecutionMode.COORDINATED and coordinated_plan is not None:
            raise InvalidTaskInput("A Subtask plan is only valid for coordinated tasks")
        if execution_mode != TaskExecutionMode.COORDINATED and (
            goal_constraints or goal_success_criteria
        ):
            raise InvalidTaskInput("Goal Contract details are only valid for coordinated tasks")
        if budget is not None:
            self._feature_gates.require(Feature.BUDGET_ADMISSION)
        task = Task.create(
            tenant_id=self._tenant_id,
            project_id=project_id,
            objective=objective,
            input=normalized_input,
            execution_mode=execution_mode,
            acceptance_criteria=acceptance_criteria,
            max_revisions=max_revisions,
            review_deadline=review_deadline,
            plan_version=(coordinated_plan.version if coordinated_plan else None),
            plan_digest=(coordinated_plan.digest if coordinated_plan else None),
            max_concurrency=(coordinated_plan.max_concurrency if coordinated_plan else 1),
            budget=budget,
        )
        subtasks: list[Subtask] = []
        dependencies: list[SubtaskDependency] = []
        with self._uow_factory() as uow:
            uow.tasks.add(task)
            if coordinated_plan is not None:
                uow.flush()
                uow.goal_contracts.add(
                    GoalContract.create(
                        task_id=task.id,
                        objective=task.objective,
                        constraints=goal_constraints,
                        success_criteria=goal_success_criteria,
                    )
                )
            if tool_authorization is not None:
                uow.flush()
                uow.tool_execution_authorizations.add(
                    ToolExecutionAuthorization.create(
                        tenant_id=self._tenant_id,
                        task_id=task.id,
                        draft=tool_authorization,
                    )
                )
            if coordinated_plan is not None:
                # The persistence models intentionally avoid ORM relationships, so
                # make the aggregate's foreign-key insertion order explicit while
                # keeping all three stages in one transaction.
                uow.flush()
                subtasks, dependencies = coordinated_plan.materialize(task.id)
                for subtask in subtasks:
                    uow.subtasks.add(subtask)
                uow.flush()
                for dependency in dependencies:
                    uow.subtask_dependencies.add(dependency)
            uow.commit()
        return TaskAggregate(
            task=task,
            subtasks=subtasks,
            dependencies=dependencies,
        )

    def get_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id)
            self._require_tenant(task)
            runs = uow.runs.list_for_task(task_id)
            attempts = uow.attempts.list_for_task(task_id)
            return TaskAggregate(
                task=task,
                runs=runs,
                attempts=attempts,
                subtasks=uow.subtasks.list_for_task(task_id),
                dependencies=uow.subtask_dependencies.list_for_task(task_id),
                handoffs=uow.handoffs.list_for_task(task_id),
            )

    def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: TaskStatus | None = None,
    ) -> list[TaskAggregate]:
        with self._uow_factory() as uow:
            tasks = uow.tasks.list(
                limit=limit,
                offset=offset,
                tenant_id=self._tenant_id,
                status=status,
            )
            if not tasks:
                return []
            task_ids = [task.id for task in tasks]
            runs_by_task = self._group_runs_by_task(uow.runs.list_for_tasks(task_ids))
            attempts_by_task = self._group_attempts_by_task(
                uow.attempts.list_for_tasks(task_ids),
                runs_by_task,
            )
            subtasks_by_task = self._group_subtasks_by_task(uow.subtasks.list_for_tasks(task_ids))
            dependencies_by_task = self._group_dependencies_by_task(
                uow.subtask_dependencies.list_for_tasks(task_ids)
            )
            handoffs_by_task = self._group_handoffs_by_task(uow.handoffs.list_for_tasks(task_ids))
            return [
                TaskAggregate(
                    task=task,
                    runs=runs_by_task.get(task.id, []),
                    attempts=attempts_by_task.get(task.id, []),
                    subtasks=subtasks_by_task.get(task.id, []),
                    dependencies=dependencies_by_task.get(task.id, []),
                    handoffs=handoffs_by_task.get(task.id, []),
                )
                for task in tasks
            ]

    def request_run(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> TaskAggregate:
        normalized_key = idempotency_key.strip() if idempotency_key is not None else None
        if idempotency_key is not None and not normalized_key:
            raise InvalidTaskInput("Idempotency-Key must not be blank")
        scope = f"request-run:{self._tenant_id}"
        request_hash = sha256(f"{scope}:{task_id}".encode()).hexdigest()

        with self._uow_factory() as uow:
            if normalized_key:
                uow.idempotency.lock(scope, normalized_key)
                existing = uow.idempotency.get(scope, normalized_key)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used for a different run request"
                        )
                    existing_task_id = UUID(str(existing.result["task_id"]))
                    task = self._get_task_or_raise(uow, existing_task_id)
                    self._require_tenant(task)
                    return TaskAggregate(
                        task=task,
                        runs=uow.runs.list_for_task(task.id),
                        attempts=uow.attempts.list_for_task(task.id),
                        subtasks=uow.subtasks.list_for_task(task.id),
                        dependencies=uow.subtask_dependencies.list_for_task(task.id),
                        handoffs=uow.handoffs.list_for_task(task.id),
                    )

            task = self._get_task_or_raise(uow, task_id, for_update=True)
            self._require_tenant(task)
            if task.execution_mode == TaskExecutionMode.COORDINATED:
                created_runs = self._coordinated_scheduler.start(uow, task)
                uow.tasks.save(task)
                if normalized_key:
                    uow.idempotency.add(
                        IdempotencyRecord.create(
                            scope=scope,
                            key=normalized_key,
                            request_hash=request_hash,
                            result={
                                "task_id": str(task.id),
                                "run_id": str(created_runs[0].id) if created_runs else None,
                            },
                        )
                    )
                uow.commit()
                return TaskAggregate(
                    task=task,
                    runs=uow.runs.list_for_task(task.id),
                    attempts=uow.attempts.list_for_task(task.id),
                    subtasks=uow.subtasks.list_for_task(task.id),
                    dependencies=uow.subtask_dependencies.list_for_task(task.id),
                    handoffs=uow.handoffs.list_for_task(task.id),
                )
            if task.execution_mode == TaskExecutionMode.FEDERATED:
                raise InvalidTaskInput(
                    "Federated Tasks must be started through the A2A delegation endpoint"
                )
            rejection = BudgetController.run_rejection(uow, task)
            if rejection is not None:
                task.wait_for_budget(rejection)
                uow.tasks.save(task)
                if normalized_key:
                    uow.idempotency.add(
                        IdempotencyRecord.create(
                            scope=scope,
                            key=normalized_key,
                            request_hash=request_hash,
                            result={"task_id": str(task.id), "run_id": None},
                        )
                    )
                uow.commit()
                return TaskAggregate(task=task)
            agent_name, agent_version = self._resolve_agent(uow)
            run = TaskRun.request(
                task_id=task.id,
                agent_id=agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=RunRole.EXECUTOR,
                revision_number=0,
            )
            task.queue(run.id)
            envelope = MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
            )
            uow.runs.add(run)
            uow.tasks.save(task)
            uow.outbox.add(envelope)
            if normalized_key:
                uow.idempotency.add(
                    IdempotencyRecord.create(
                        scope=scope,
                        key=normalized_key,
                        request_hash=request_hash,
                        result={"task_id": str(task.id), "run_id": str(run.id)},
                    )
                )
            uow.commit()
        return TaskAggregate(task=task, runs=[run])

    def cancel_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            self._require_tenant(task)
            task.cancel()
            if task.execution_mode == TaskExecutionMode.COORDINATED:
                for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
                    subtask.cancel()
                    uow.subtasks.save(subtask)
                for run in uow.runs.list_for_task(task.id):
                    if run.status in {
                        RunStatus.QUEUED,
                        RunStatus.RUNNING,
                        RunStatus.PAUSE_REQUESTED,
                        RunStatus.PAUSED,
                        RunStatus.WAITING_REMOTE,
                    }:
                        run.cancel()
                        uow.runs.save(run)
                        attempt = uow.attempts.latest_for_run(run.id, for_update=True)
                        if attempt is not None and attempt.status == AttemptStatus.RUNNING:
                            BudgetController.release_attempt(task, attempt)
                            QuotaController.release_attempt(uow, attempt)
                            attempt.cancel()
                            uow.attempts.save(attempt)
            if task.current_run_id is not None:
                run = uow.runs.get(task.current_run_id, for_update=True)
                if run is not None and run.status in {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.PAUSE_REQUESTED,
                    RunStatus.PAUSED,
                    RunStatus.WAITING_REMOTE,
                }:
                    run.cancel()
                    uow.runs.save(run)
                attempt = uow.attempts.latest_for_run(task.current_run_id, for_update=True)
                if attempt is not None and attempt.status == AttemptStatus.RUNNING:
                    BudgetController.release_attempt(task, attempt)
                    QuotaController.release_attempt(uow, attempt)
                    attempt.cancel()
                    uow.attempts.save(attempt)
            uow.tasks.save(task)
            uow.commit()
        return self.get_task(task_id)

    def pause_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            self._require_tenant(task)
            run = self._active_run_or_raise(uow, task)
            valid_pairs = {
                (TaskStatus.READY, RunStatus.QUEUED),
                (TaskStatus.RUNNING, RunStatus.RUNNING),
                (TaskStatus.PAUSE_REQUESTED, RunStatus.PAUSE_REQUESTED),
                (TaskStatus.PAUSED, RunStatus.PAUSED),
            }
            if (task.status, run.status) not in valid_pairs:
                raise InvalidTaskTransition(
                    f"Cannot pause task {task.id} with task/run statuses "
                    f"{task.status.value}/{run.status.value}"
                )
            previous_status = task.status
            task.request_pause(run.id)
            run.request_pause()
            if task.status != previous_status:
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.outbox.add(
                    self._task_control_event(
                        task,
                        run,
                        action=(
                            "paused" if task.status == TaskStatus.PAUSED else "pause-requested"
                        ),
                    )
                )
                uow.commit()
        return self.get_task(task_id)

    def resume_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            self._require_tenant(task)
            run = self._active_run_or_raise(uow, task)
            if (task.status, run.status) != (TaskStatus.PAUSED, RunStatus.PAUSED):
                if (
                    run.resumed_at is not None
                    and run.pause_requested_at is not None
                    and run.resumed_at >= run.pause_requested_at
                    and task.status
                    in {
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELED,
                    }
                ):
                    return TaskAggregate(
                        task=task,
                        runs=uow.runs.list_for_task(task.id),
                        attempts=uow.attempts.list_for_task(task.id),
                    )
                raise InvalidTaskTransition(
                    f"Cannot resume task {task.id} with task/run statuses "
                    f"{task.status.value}/{run.status.value}"
                )

            task.resume(run.id)
            run.resume()
            uow.tasks.save(task)
            uow.runs.save(run)
            uow.outbox.add(
                MessageEnvelope.run_requested(
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    run_id=run.id,
                )
            )
            uow.outbox.add(self._task_control_event(task, run, action="resumed"))
            uow.commit()
        return self.get_task(task_id)

    @staticmethod
    def _active_run_or_raise(uow: Any, task: Task) -> TaskRun:
        if task.current_run_id is None:
            raise InvalidTaskTransition(f"Task {task.id} has no active Run")
        run = uow.runs.get(task.current_run_id, for_update=True)
        if run is None or run.task_id != task.id:
            raise InvalidTaskTransition(f"Task {task.id} active Run is unavailable")
        return run

    @staticmethod
    def _group_runs_by_task(runs: list[TaskRun]) -> dict[UUID, list[TaskRun]]:
        grouped: dict[UUID, list[TaskRun]] = {}
        for run in runs:
            grouped.setdefault(run.task_id, []).append(run)
        return grouped

    @staticmethod
    def _group_attempts_by_task(
        attempts: list[TaskAttempt],
        runs_by_task: dict[UUID, list[TaskRun]],
    ) -> dict[UUID, list[TaskAttempt]]:
        task_by_run = {run.id: task_id for task_id, runs in runs_by_task.items() for run in runs}
        grouped: dict[UUID, list[TaskAttempt]] = {}
        for attempt in attempts:
            task_id = task_by_run.get(attempt.run_id)
            if task_id is not None:
                grouped.setdefault(task_id, []).append(attempt)
        return grouped

    @staticmethod
    def _group_subtasks_by_task(subtasks: list[Subtask]) -> dict[UUID, list[Subtask]]:
        grouped: dict[UUID, list[Subtask]] = {}
        for subtask in subtasks:
            grouped.setdefault(subtask.task_id, []).append(subtask)
        return grouped

    @staticmethod
    def _group_dependencies_by_task(
        dependencies: list[SubtaskDependency],
    ) -> dict[UUID, list[SubtaskDependency]]:
        grouped: dict[UUID, list[SubtaskDependency]] = {}
        for dependency in dependencies:
            grouped.setdefault(dependency.task_id, []).append(dependency)
        return grouped

    @staticmethod
    def _group_handoffs_by_task(handoffs: list[Handoff]) -> dict[UUID, list[Handoff]]:
        grouped: dict[UUID, list[Handoff]] = {}
        for handoff in handoffs:
            grouped.setdefault(handoff.task_id, []).append(handoff)
        return grouped

    @staticmethod
    def _task_control_event(task: Task, run: TaskRun, *, action: str) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name=f"agentmesh.task.{action}",
            tenant_id=task.tenant_id,
            aggregate_id=task.id,
            payload={
                "task_id": str(task.id),
                "run_id": str(run.id),
                "task_status": task.status.value,
                "run_status": run.status.value,
            },
        )

    @staticmethod
    def _get_task_or_raise(uow: Any, task_id: UUID, *, for_update: bool = False) -> Task:
        task = uow.tasks.get(task_id, for_update=for_update)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    def _require_tenant(self, task: Task) -> None:
        if task.tenant_id != self._tenant_id:
            raise TaskNotFound(task.id)

    def _resolve_agent(self, uow: Any) -> tuple[str, AgentVersion]:
        return self._resolve_agent_by_name(uow, self._tenant_id, self._agent_id)

    @staticmethod
    def _resolve_agent_by_name(
        uow: Any, tenant_id: str, configured_name: str
    ) -> tuple[str, AgentVersion]:
        agent_name = normalize_agent_name(configured_name)
        definition = uow.agent_definitions.get_by_name(tenant_id, agent_name, for_update=True)
        if definition is None or definition.default_version_id is None:
            raise AgentUnavailable(f"Agent {agent_name} has no published default version")
        agent_version = uow.agent_versions.get(definition.default_version_id, for_update=True)
        if (
            agent_version is None
            or agent_version.status != AgentVersionStatus.PUBLISHED
            or not agent_version.content_digest
        ):
            raise AgentUnavailable(f"Agent {agent_name} default version is unavailable")
        return definition.name, agent_version


class RunExecutionService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workflow_runner: WorkflowRunner,
        worker_id: str,
        consumer_name: str,
        lease_duration: timedelta,
        executor_agent_id: str = "demo-agent",
        reviewer_agent_id: str = "demo-reviewer",
        supervisor_agent_id: str = "demo-supervisor",
        lease_renewal_interval: timedelta | None = None,
        feature_gates: FeatureGateSet | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workflow_runner = workflow_runner
        self._worker_id = worker_id
        self._consumer_name = consumer_name
        self._lease_duration = lease_duration
        self._executor_agent_id = executor_agent_id
        self._reviewer_agent_id = reviewer_agent_id
        self._coordinated_scheduler = CoordinatedScheduler(supervisor_agent_id=supervisor_agent_id)
        self._lease_renewal_interval = lease_renewal_interval or self._default_renewal_interval(
            lease_duration
        )
        self._feature_gates = feature_gates or FeatureGateSet.from_config("minimal")

    def process(self, envelope: MessageEnvelope) -> bool:
        task_id, run_id = self._validate(envelope)
        leased = self._acquire(envelope, task_id=task_id, run_id=run_id)
        if leased is None:
            return False
        task, run, attempt = leased

        renewer = _AttemptLeaseRenewer(
            service=self,
            run_id=run.id,
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
            interval=self._lease_renewal_interval,
        )
        try:
            work_item = self._workflow_work_item(task, run)
            with renewer:
                if work_item is None:
                    result = self._workflow_runner.run(task, run, attempt)
                else:
                    result = self._workflow_runner.run(task, run, attempt, work_item=work_item)
        except Exception as exc:
            error = f"Workflow execution failed: {type(exc).__name__}"
            self._finalize_failure(envelope, task_id, run_id, attempt.id, error)
            return True

        try:
            self._finalize_success(
                envelope,
                task_id,
                run_id,
                attempt.id,
                result.output,
                usage_records=result.usage_records,
            )
        except (InvalidTaskInput, AgentUnavailable) as exc:
            self._finalize_failure(
                envelope,
                task_id,
                run_id,
                attempt.id,
                f"Execution finalization failed: {type(exc).__name__}",
            )
        return True

    def _acquire(
        self,
        envelope: MessageEnvelope,
        *,
        task_id: UUID,
        run_id: UUID,
    ) -> tuple[Task, TaskRun, TaskAttempt] | None:
        with self._uow_factory() as uow:
            if uow.inbox.contains(
                envelope.tenant_id,
                self._consumer_name,
                envelope.message_id,
            ):
                return None

            task = TaskApplicationService._get_task_or_raise(uow, task_id, for_update=True)
            run = uow.runs.get(run_id, for_update=True)
            if run is None or run.task_id != task.id:
                raise InvalidMessage("RunRequested references an unknown task run")
            if task.tenant_id != envelope.tenant_id:
                raise InvalidMessage("RunRequested tenant does not own the referenced task")

            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
                uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
                uow.commit()
                return None

            if (task.status, run.status) == (TaskStatus.PAUSED, RunStatus.PAUSED):
                uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
                uow.commit()
                return None
            if TaskStatus.PAUSED == task.status or RunStatus.PAUSED == run.status:
                raise InvalidMessage("RunRequested references inconsistent paused task state")

            latest = uow.attempts.latest_for_run(run.id, for_update=True)
            now = utc_now()
            if latest is not None and latest.status == AttemptStatus.RUNNING:
                if latest.lease_expires_at > now:
                    raise RunLeaseUnavailable(
                        f"Run {run.id} is leased by worker {latest.worker_id}"
                    )
                latest.expire()
                BudgetController.release_attempt(task, latest)
                QuotaController.release_attempt(uow, latest)
                uow.attempts.save(latest)
                uow.tasks.save(task)

            if (task.status, run.status) == (
                TaskStatus.PAUSE_REQUESTED,
                RunStatus.PAUSE_REQUESTED,
            ):
                task.mark_paused(run.id)
                run.mark_paused()
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.outbox.add(self._task_paused_event(task, run, causation_id=envelope.message_id))
                uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
                uow.commit()
                return None
            if task.status == TaskStatus.PAUSE_REQUESTED or run.status == RunStatus.PAUSE_REQUESTED:
                raise InvalidMessage("RunRequested references inconsistent pause request state")

            rejection = BudgetController.attempt_rejection(uow, task, now=now)
            if rejection is not None:
                run.cancel()
                if run.subtask_id is not None:
                    subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                    if subtask is not None:
                        subtask.cancel()
                        uow.subtasks.save(subtask)
                task.wait_for_budget(rejection)
                uow.tasks.save(task)
                uow.runs.save(run)
                self._cancel_coordinated_siblings(uow, task, except_run_id=run.id)
                uow.tasks.save(task)
                uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
                uow.commit()
                return None

            if run.status == RunStatus.QUEUED:
                if run.subtask_id is not None:
                    if (
                        task.execution_mode != TaskExecutionMode.COORDINATED
                        or task.status != TaskStatus.RUNNING
                    ):
                        raise InvalidMessage("Subtask Run is not active for a coordinated Task")
                    subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                    if subtask is None or subtask.task_id != task.id:
                        raise InvalidMessage("RunRequested references an unknown Subtask")
                    subtask.start(run.id)
                    uow.subtasks.save(subtask)
                elif run.role == RunRole.REVIEWER:
                    task.start_review(run.id)
                elif run.role == RunRole.SUPERVISOR:
                    if (
                        task.execution_mode != TaskExecutionMode.COORDINATED
                        or task.status != TaskStatus.RUNNING
                        or task.current_run_id != run.id
                    ):
                        raise InvalidMessage("Supervisor Run is not active for the Task")
                else:
                    task.start(run.id)
                run.start()
                if run.subtask_id is None:
                    uow.tasks.save(task)
                uow.runs.save(run)
            elif run.status == RunStatus.RUNNING:
                expected_task_status = (
                    TaskStatus.REVIEWING if run.role == RunRole.REVIEWER else TaskStatus.RUNNING
                )
                if task.status != expected_task_status:
                    raise InvalidMessage("RunRequested references inconsistent task state")
                if run.subtask_id is not None:
                    subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                    if (
                        subtask is None
                        or subtask.task_id != task.id
                        or subtask.status != SubtaskStatus.RUNNING
                        or subtask.current_run_id != run.id
                    ):
                        raise InvalidMessage("RunRequested references inconsistent Subtask state")
                elif run.role == RunRole.SUPERVISOR and task.current_run_id != run.id:
                    raise InvalidMessage("RunRequested references an inactive Supervisor Run")
            else:
                raise InvalidMessage("RunRequested references inconsistent task state")

            attempt = TaskAttempt.lease(
                run_id=run.id,
                worker_id=self._worker_id,
                fencing_token=(latest.fencing_token + 1 if latest else 1),
                lease_expires_at=now + self._lease_duration,
                reserved_tokens=(task.budget.token_reservation_per_attempt if task.budget else 0),
                reserved_cost_micros=(
                    task.budget.cost_reservation_micros_per_attempt if task.budget else 0
                ),
            )
            BudgetController.reserve_attempt(task, attempt)
            uow.attempts.add(attempt)
            if self._feature_gates.is_enabled(Feature.QUOTA_ADMISSION):
                try:
                    QuotaController.reserve_attempt(uow, task, attempt)
                except QuotaAdmissionRejected as exc:
                    raise RunLeaseUnavailable(str(exc)) from exc
            if task.budget is not None:
                uow.tasks.save(task)
            uow.commit()
            return task, run, attempt

    def _renew_attempt_lease(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        lease_token: UUID,
    ) -> bool:
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            latest = uow.attempts.latest_for_run(run_id, for_update=True)
            now = utc_now()
            if attempt is None or latest is None or latest.id != attempt.id:
                return False
            if attempt.status != AttemptStatus.RUNNING:
                return False
            if attempt.lease_expires_at <= now:
                return False
            attempt.renew(
                worker_id=self._worker_id,
                lease_token=lease_token,
                lease_expires_at=now + self._lease_duration,
                heartbeat_at=now,
            )
            uow.attempts.save(attempt)
            uow.commit()
            return True

    def _finalize_success(
        self,
        envelope: MessageEnvelope,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        output: dict[str, Any],
        usage_records: tuple[UsageRecord, ...] = (),
    ) -> None:
        with self._uow_factory() as uow:
            task, run, attempt = self._load_finalization_state(uow, task_id, run_id, attempt_id)
            self._persist_usage_records(uow, task, run, usage_records)
            budget_rejection = BudgetController.settle_attempt(task, attempt, usage_records)
            QuotaController.release_attempt(uow, attempt)
            if task.status == TaskStatus.CANCELED or run.status == RunStatus.CANCELED:
                if attempt.status == AttemptStatus.RUNNING:
                    attempt.cancel()
                    uow.attempts.save(attempt)
                if task.budget is not None:
                    uow.tasks.save(task)
                    uow.attempts.save(attempt)
            elif (task.status, run.status) == (
                TaskStatus.PAUSE_REQUESTED,
                RunStatus.PAUSE_REQUESTED,
            ):
                task.mark_paused(run.id)
                run.mark_paused()
                attempt.pause()
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.attempts.save(attempt)
                uow.outbox.add(self._task_paused_event(task, run, causation_id=envelope.message_id))
            else:
                run.succeed(output)
                attempt.succeed()
                if budget_rejection is not None:
                    if task.execution_mode == TaskExecutionMode.COORDINATED:
                        if run.subtask_id is not None:
                            subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                            if subtask is None or subtask.task_id != task.id:
                                raise InvalidTaskInput("Coordinated Run lost its Subtask binding")
                            subtask.complete(run.id, output)
                            uow.subtasks.save(subtask)
                        task.wait_for_budget(
                            budget_rejection,
                            candidate_output=(output if run.subtask_id is None else None),
                        )
                        self._cancel_coordinated_siblings(uow, task, except_run_id=run.id)
                    else:
                        task.wait_for_budget(
                            budget_rejection,
                            candidate_output=(
                                output
                                if task.execution_mode == TaskExecutionMode.DIRECT
                                or run.role == RunRole.EXECUTOR
                                else None
                            ),
                        )
                elif task.execution_mode == TaskExecutionMode.COORDINATED:
                    if run.subtask_id is not None:
                        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                        if subtask is None or subtask.task_id != task.id:
                            raise InvalidTaskInput("Coordinated Run lost its Subtask binding")
                        subtask.complete(run.id, output)
                        uow.subtasks.save(subtask)
                        self._coordinated_scheduler.schedule(uow, task)
                    elif run.role == RunRole.SUPERVISOR:
                        task.complete(run.id, output)
                    else:
                        raise InvalidTaskInput("Coordinated Task received an invalid Run role")
                elif task.execution_mode == TaskExecutionMode.DIRECT:
                    task.complete(run.id, output)
                elif run.role == RunRole.EXECUTOR:
                    rejection = BudgetController.run_rejection(uow, task)
                    if rejection is not None:
                        task.wait_for_budget(rejection, candidate_output=output)
                    else:
                        reviewer_name, reviewer_version = (
                            TaskApplicationService._resolve_agent_by_name(
                                uow, task.tenant_id, self._reviewer_agent_id
                            )
                        )
                        reviewer_run = TaskRun.request(
                            task.id,
                            reviewer_name,
                            agent_version_id=reviewer_version.id,
                            agent_version_digest=reviewer_version.content_digest,
                            role=RunRole.REVIEWER,
                            revision_number=run.revision_number,
                        )
                        task.queue_review(run.id, output, reviewer_run.id)
                        uow.runs.add(reviewer_run)
                        uow.outbox.add(
                            MessageEnvelope.run_requested(
                                tenant_id=task.tenant_id,
                                task_id=task.id,
                                run_id=reviewer_run.id,
                            )
                        )
                else:
                    decision = ReviewDecision.from_output(output, task.acceptance_criteria)
                    revision_run = None
                    evaluated_at = utc_now()
                    within_deadline = (
                        task.review_deadline is None or evaluated_at < task.review_deadline
                    )
                    if (
                        not decision.accepted
                        and within_deadline
                        and task.revision_count < task.max_revisions
                    ):
                        rejection = BudgetController.run_rejection(uow, task)
                        if rejection is not None:
                            task.latest_review = decision.to_dict()
                            task.wait_for_budget(rejection)
                        else:
                            executor_name, executor_version = (
                                TaskApplicationService._resolve_agent_by_name(
                                    uow, task.tenant_id, self._executor_agent_id
                                )
                            )
                            revision_run = TaskRun.request(
                                task.id,
                                executor_name,
                                agent_version_id=executor_version.id,
                                agent_version_digest=executor_version.content_digest,
                                role=RunRole.EXECUTOR,
                                revision_number=task.revision_count + 1,
                            )
                    if task.status != TaskStatus.WAITING_APPROVAL:
                        task.apply_review(
                            run.id,
                            decision,
                            revision_run.id if revision_run is not None else None,
                            evaluated_at=evaluated_at,
                        )
                    if revision_run is not None:
                        uow.runs.add(revision_run)
                        uow.outbox.add(
                            MessageEnvelope.run_requested(
                                tenant_id=task.tenant_id,
                                task_id=task.id,
                                run_id=revision_run.id,
                            )
                        )
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.attempts.save(attempt)
            uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
            uow.commit()

    @staticmethod
    def _persist_usage_records(
        uow: Any,
        task: Task,
        run: TaskRun,
        records: tuple[UsageRecord, ...],
    ) -> None:
        for record in records:
            origin_attempt = uow.attempts.get(record.attempt_id)
            if (
                record.tenant_id != task.tenant_id
                or record.task_id != task.id
                or record.run_id != run.id
                or origin_attempt is None
                or origin_attempt.run_id != run.id
                or origin_attempt.trace_id != record.trace_id
            ):
                raise TaskExecutionFailed(
                    task.id,
                    f"Usage record {record.id} does not belong to this Task Run",
                )
            uow.usage_records.add_if_absent(record)

    @staticmethod
    def _task_paused_event(
        task: Task,
        run: TaskRun,
        *,
        causation_id: UUID,
    ) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name="agentmesh.task.paused",
            tenant_id=task.tenant_id,
            aggregate_id=task.id,
            causation_id=causation_id,
            producer="agentmesh-execution-worker",
            payload={
                "task_id": str(task.id),
                "run_id": str(run.id),
                "task_status": task.status.value,
                "run_status": run.status.value,
            },
        )

    def _finalize_failure(
        self,
        envelope: MessageEnvelope,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        error: str,
    ) -> None:
        with self._uow_factory() as uow:
            task, run, attempt = self._load_finalization_state(uow, task_id, run_id, attempt_id)
            BudgetController.release_attempt(task, attempt)
            QuotaController.release_attempt(uow, attempt)
            if task.status == TaskStatus.CANCELED or run.status == RunStatus.CANCELED:
                if attempt.status == AttemptStatus.RUNNING:
                    attempt.cancel()
                    uow.attempts.save(attempt)
            else:
                if task.execution_mode == TaskExecutionMode.COORDINATED:
                    if run.subtask_id is not None:
                        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
                        if subtask is None or subtask.task_id != task.id:
                            raise InvalidTaskInput("Coordinated Run lost its Subtask binding")
                        subtask.fail(run.id, error)
                        uow.subtasks.save(subtask)
                        task.fail_coordination(error)
                        self._cancel_coordinated_siblings(uow, task, except_run_id=run.id)
                    else:
                        task.fail(run.id, error)
                else:
                    task.fail(run.id, error)
                run.fail(error)
                attempt.fail(error)
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.attempts.save(attempt)
            if task.budget is not None:
                uow.tasks.save(task)
            uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
            uow.commit()

    def _workflow_work_item(self, task: Task, run: TaskRun) -> WorkflowWorkItem | None:
        if task.execution_mode != TaskExecutionMode.COORDINATED:
            return None
        with self._uow_factory() as uow:
            objective, input = self._coordinated_scheduler.work_item_input(uow, task, run)
        return WorkflowWorkItem(objective=objective, input=input)

    def _cancel_coordinated_siblings(self, uow: Any, task: Task, *, except_run_id: UUID) -> None:
        for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
            if subtask.status not in {
                SubtaskStatus.COMPLETED,
                SubtaskStatus.FAILED,
                SubtaskStatus.CANCELED,
            }:
                subtask.cancel()
                uow.subtasks.save(subtask)
        for sibling in uow.runs.list_for_task(task.id):
            if sibling.id == except_run_id or sibling.status not in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.PAUSE_REQUESTED,
                RunStatus.PAUSED,
            }:
                continue
            sibling.cancel()
            uow.runs.save(sibling)
            sibling_attempt = uow.attempts.latest_for_run(sibling.id, for_update=True)
            if sibling_attempt is not None and sibling_attempt.status == AttemptStatus.RUNNING:
                BudgetController.release_attempt(task, sibling_attempt)
                QuotaController.release_attempt(uow, sibling_attempt)
                sibling_attempt.cancel()
                uow.attempts.save(sibling_attempt)

    @staticmethod
    def _load_finalization_state(
        uow: Any,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
    ) -> tuple[Task, TaskRun, TaskAttempt]:
        task = TaskApplicationService._get_task_or_raise(uow, task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        latest = uow.attempts.latest_for_run(run_id, for_update=True)
        if run is None or attempt is None:
            raise TaskExecutionFailed(task_id, "Execution state disappeared before finalization")
        if latest is None or latest.id != attempt.id:
            raise RunLeaseUnavailable(f"Attempt {attempt_id} no longer owns run {run_id}")
        if attempt.status != AttemptStatus.RUNNING:
            if (
                attempt.status == AttemptStatus.CANCELED
                and task.status
                in {TaskStatus.CANCELED, TaskStatus.FAILED, TaskStatus.WAITING_APPROVAL}
                and run.status == RunStatus.CANCELED
            ):
                return task, run, attempt
            raise RunLeaseUnavailable(
                f"Attempt {attempt_id} cannot finalize from status {attempt.status.value}"
            )
        if attempt.lease_expires_at <= utc_now():
            raise RunLeaseUnavailable(f"Attempt {attempt_id} lease expired before finalization")
        return task, run, attempt

    @staticmethod
    def _validate(envelope: MessageEnvelope) -> tuple[UUID, UUID]:
        if (
            envelope.schema_name != RUN_REQUESTED_SCHEMA
            or envelope.schema_version != RUN_REQUESTED_VERSION
        ):
            raise InvalidMessage(
                f"Unsupported message schema {envelope.schema_name}@{envelope.schema_version}"
            )
        try:
            task_id = UUID(str(envelope.payload["task_id"]))
            run_id = UUID(str(envelope.payload["run_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidMessage(
                "RunRequested payload must contain UUID task_id and run_id"
            ) from exc
        if envelope.correlation_id != task_id:
            raise InvalidMessage("RunRequested correlation_id must equal task_id")
        return task_id, run_id

    @staticmethod
    def _default_renewal_interval(lease_duration: timedelta) -> timedelta:
        seconds = max(0.001, lease_duration.total_seconds() / 3)
        return timedelta(seconds=seconds)


class _AttemptLeaseRenewer:
    def __init__(
        self,
        *,
        service: RunExecutionService,
        run_id: UUID,
        attempt_id: UUID,
        lease_token: UUID,
        interval: timedelta,
    ) -> None:
        self._service = service
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._lease_token = lease_token
        self._interval_seconds = max(0.001, interval.total_seconds())
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentmesh-lease-renewer-{attempt_id}",
            daemon=True,
        )

    def __enter__(self) -> _AttemptLeaseRenewer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                renewed = self._service._renew_attempt_lease(
                    run_id=self._run_id,
                    attempt_id=self._attempt_id,
                    lease_token=self._lease_token,
                )
            except Exception:
                logger.warning("Attempt lease renewal failed", exc_info=True)
                continue
            if not renewed:
                logger.info("Attempt %s lease renewal stopped", self._attempt_id)
                return
