from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory, WorkflowRunner
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
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    IdempotencyRecord,
    InboxMessage,
    MessageEnvelope,
)
from agentmesh.domain.registry import AgentVersion, AgentVersionStatus, normalize_agent_name
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunStatus,
    Task,
    TaskAggregate,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    utc_now,
)
from agentmesh.domain.tools import WORKSPACE_READ_TOOL_KEY, ToolCallRequest
from agentmesh.features import Feature, FeatureGateSet


class TaskApplicationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        agent_id: str,
        tenant_id: str,
        feature_gates: FeatureGateSet | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates or FeatureGateSet.from_config("minimal")

    def create_task(
        self,
        objective: str,
        input: dict[str, Any] | None = None,
    ) -> TaskAggregate:
        normalized_input = dict(input or {})
        tool_request = ToolCallRequest.from_task_input(normalized_input)
        if tool_request is not None:
            self._feature_gates.require(Feature.MCP_READ_TOOLS)
            if tool_request.tool_key != WORKSPACE_READ_TOOL_KEY:
                raise InvalidToolRequest(
                    f"Tool '{tool_request.tool_key}' is not in the current allowlist"
                )
        task = Task.create(tenant_id=self._tenant_id, objective=objective, input=normalized_input)
        with self._uow_factory() as uow:
            uow.tasks.add(task)
            uow.commit()
        return TaskAggregate(task=task)

    def get_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id)
            self._require_tenant(task)
            runs = uow.runs.list_for_task(task_id)
            attempts = uow.attempts.list_for_task(task_id)
            return TaskAggregate(task=task, runs=runs, attempts=attempts)

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
            return [
                TaskAggregate(
                    task=task,
                    runs=uow.runs.list_for_task(task.id),
                    attempts=uow.attempts.list_for_task(task.id),
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
                    )

            task = self._get_task_or_raise(uow, task_id, for_update=True)
            self._require_tenant(task)
            agent_name, agent_version = self._resolve_agent(uow)
            run = TaskRun.request(
                task_id=task.id,
                agent_id=agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
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
            if task.current_run_id is not None:
                run = uow.runs.get(task.current_run_id, for_update=True)
                if run is not None and run.status in {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.PAUSE_REQUESTED,
                    RunStatus.PAUSED,
                }:
                    run.cancel()
                    uow.runs.save(run)
                attempt = uow.attempts.latest_for_run(task.current_run_id, for_update=True)
                if attempt is not None and attempt.status == AttemptStatus.RUNNING:
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
        agent_name = normalize_agent_name(self._agent_id)
        definition = uow.agent_definitions.get_by_name(self._tenant_id, agent_name, for_update=True)
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
    ) -> None:
        self._uow_factory = uow_factory
        self._workflow_runner = workflow_runner
        self._worker_id = worker_id
        self._consumer_name = consumer_name
        self._lease_duration = lease_duration

    def process(self, envelope: MessageEnvelope) -> bool:
        task_id, run_id = self._validate(envelope)
        leased = self._acquire(envelope, task_id=task_id, run_id=run_id)
        if leased is None:
            return False
        task, run, attempt = leased

        try:
            output = self._workflow_runner.run(task, run)
        except Exception as exc:
            error = f"Workflow execution failed: {type(exc).__name__}"
            self._finalize_failure(envelope, task_id, run_id, attempt.id, error)
            return True

        self._finalize_success(envelope, task_id, run_id, attempt.id, output)
        return True

    def _acquire(
        self,
        envelope: MessageEnvelope,
        *,
        task_id: UUID,
        run_id: UUID,
    ) -> tuple[Task, TaskRun, TaskAttempt] | None:
        with self._uow_factory() as uow:
            if uow.inbox.contains(self._consumer_name, envelope.message_id):
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
                uow.attempts.save(latest)

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

            if run.status == RunStatus.QUEUED:
                task.start(run.id)
                run.start()
                uow.tasks.save(task)
                uow.runs.save(run)
            elif run.status != RunStatus.RUNNING or task.status != TaskStatus.RUNNING:
                raise InvalidMessage("RunRequested references inconsistent task state")

            attempt = TaskAttempt.lease(
                run_id=run.id,
                worker_id=self._worker_id,
                fencing_token=(latest.fencing_token + 1 if latest else 1),
                lease_expires_at=now + self._lease_duration,
            )
            uow.attempts.add(attempt)
            uow.commit()
            return task, run, attempt

    def _finalize_success(
        self,
        envelope: MessageEnvelope,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        output: dict[str, Any],
    ) -> None:
        with self._uow_factory() as uow:
            task, run, attempt = self._load_finalization_state(uow, task_id, run_id, attempt_id)
            if task.status == TaskStatus.CANCELED or run.status == RunStatus.CANCELED:
                if attempt.status == AttemptStatus.RUNNING:
                    attempt.cancel()
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
                task.complete(run.id, output)
                run.succeed(output)
                attempt.succeed()
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.attempts.save(attempt)
            uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
            uow.commit()

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
            if task.status == TaskStatus.CANCELED or run.status == RunStatus.CANCELED:
                if attempt.status == AttemptStatus.RUNNING:
                    attempt.cancel()
                    uow.attempts.save(attempt)
            else:
                task.fail(run.id, error)
                run.fail(error)
                attempt.fail(error)
                uow.tasks.save(task)
                uow.runs.save(run)
                uow.attempts.save(attempt)
            uow.inbox.add(InboxMessage.processed(self._consumer_name, envelope))
            uow.commit()

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
                and task.status == TaskStatus.CANCELED
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
