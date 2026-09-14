"""Task-first acquisition of coordinated managed Runtime deliveries.

This module owns only the short delivery-acquisition transaction.  Provider
calls, assignment preparation and terminal convergence deliberately live in
the following c2f slices.  In particular, a returned lease is a detached
value object; no aggregate entity is allowed to cross this boundary.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.authority_cohorts import validate_builtin_managed_runtime_version
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_barrier import release_preboundary_in_uow
from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    CoordinatedDeliveryResult,
    CoordinatedDeliveryResultKind,
    RecoveryCrossedProof,
    assignment_projection_digest,
    ownership_digest,
)
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.application.quota_services import QuotaAdmissionRejected, QuotaController
from agentmesh.application.runtime_services import validate_runtime_assignment_chain
from agentmesh.application.runtime_snapshots import parse_assignment_payload
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.domain.coordination import (
    TERMINAL_SUBTASK_STATUSES,
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidMessage,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    InboxMessage,
    MessageEnvelope,
)
from agentmesh.domain.runtime_execution import RuntimeExecution
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk.assignment import RuntimeAssignment
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.common import RuntimeContractError
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor

_DRAIN_NAMESPACE = "coordination-runtime-drain:"
_MAX_LEASE_DURATION = timedelta(days=7)
_PLAN_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CoordinatedRuntimeDeliveryAcquisitionService:
    """Classify and acquire exactly one coordinated Runtime delivery."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        worker_id: str,
        consumer_name: str,
        lease_duration: timedelta,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        feature_gates: FeatureGateSet | None = None,
        work_item_builder: CanonicalWorkItemBuilder | None = None,
    ) -> None:
        if type(worker_id) is not str or not worker_id or worker_id != worker_id.strip():
            raise InvalidTaskInput("Coordinated delivery worker ID is invalid")
        if (
            type(consumer_name) is not str
            or not consumer_name
            or consumer_name != consumer_name.strip()
        ):
            raise InvalidTaskInput("Coordinated delivery consumer name is invalid")
        if (
            type(lease_duration) is not timedelta
            or lease_duration <= timedelta(0)
            or lease_duration > _MAX_LEASE_DURATION
        ):
            raise InvalidTaskInput("Coordinated delivery lease duration is invalid")
        self._uow_factory = uow_factory
        self._worker_id = worker_id
        self._consumer_name = consumer_name
        self._lease_duration = lease_duration
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        self._feature_gates = feature_gates or FeatureGateSet.from_config("minimal")
        if work_item_builder is None:
            raise InvalidTaskInput(
                "Coordinated delivery requires an explicit CanonicalWorkItemBuilder"
            )
        self._work_item_builder = work_item_builder

    def classify_and_acquire(
        self,
        envelope: MessageEnvelope,
        *,
        now: datetime | None = None,
    ) -> CoordinatedDeliveryResult:
        """Validate, lock, classify and (when safe) lease one delivery.

        Envelope decoding is intentionally completely pure and precedes UoW
        creation.  Once a UoW is entered, the Task ``FOR UPDATE`` is always the
        first repository operation; the aggregate expansion continues in that
        same UoW.
        """
        task_id, run_id, timestamp = self._validate_envelope(envelope, now=now)
        with self._uow_factory() as uow:
            # This must remain the first repository call in c2f3.
            task = uow.tasks.get(task_id, for_update=True)
            if task is None or type(task) is not Task or task.id != task_id:
                raise InvalidMessage("RunRequested references an unknown task")
            if task.tenant_id != envelope.tenant_id:
                raise InvalidMessage("RunRequested tenant does not own the referenced task")
            if task.execution_mode is not TaskExecutionMode.COORDINATED:
                return CoordinatedDeliveryResult.without_lease(
                    CoordinatedDeliveryResultKind.NOT_APPLICABLE
                )

            aggregate = self._aggregate_locker.lock_after_task(
                uow, task, tenant_id=envelope.tenant_id, task_id=task_id
            )
            self._validate_cohort(aggregate, task_id, envelope.tenant_id)
            if aggregate.cohort.runtime_authority != "managed":
                return CoordinatedDeliveryResult.without_lease(
                    CoordinatedDeliveryResultKind.NOT_APPLICABLE
                )

            # Inbox is checked only after the complete aggregate lock.  A
            # processed message is a read-only replay and never commits.
            if uow.inbox.contains(
                envelope.tenant_id, self._consumer_name, envelope.message_id
            ):
                return CoordinatedDeliveryResult.without_lease(
                    CoordinatedDeliveryResultKind.REPLAY_PROCESSED
                )

            run = self._run(aggregate, run_id)
            self._validate_run_binding(aggregate, run, task_id, envelope.tenant_id)
            boundary = aggregate.boundary_classifications.get(run.id)
            if boundary is None:
                raise RuntimeExecutionConflict("Coordinated delivery boundary is missing")

            # A PREPARED snapshot is durable Assignment authority. Validate it
            # before looking at lease freshness so tampering cannot be hidden
            # behind an IN_PROGRESS replay.
            prepared_assignment = None
            prepared_work_item = None
            if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
                prepared_execution = self._execution_for(aggregate, run)
                if prepared_execution is None:
                    raise RuntimeExecutionConflict(
                        "Prepared delivery lacks Runtime execution"
                    )
                prepared_snapshot = aggregate.assignment_snapshots_by_execution.get(
                    prepared_execution.id
                )
                if prepared_snapshot is None:
                    raise RuntimeExecutionConflict(
                        "Prepared delivery Assignment snapshot is incomplete"
                    )
                prepared_assignment = self._validate_prepared_assignment(
                    aggregate,
                    run,
                    prepared_execution,
                    prepared_snapshot,
                    envelope.tenant_id,
                )
                prepared_work_item = self._work_item_from_assignment(prepared_assignment)

            # Terminal/reconciliation messages are safe terminal acquisition
            # outcomes.  Crossed outcomes are never consumed here.
            if boundary in {
                CoordinationRuntimeBoundary.KNOWN_TERMINAL,
                CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
            }:
                self._consume(uow, envelope, timestamp)
                uow.commit()
                return CoordinatedDeliveryResult.without_lease(
                    CoordinatedDeliveryResultKind.REPLAY_PROCESSED
                )

            if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
                return self._classify_crossed(
                    uow, aggregate, run, envelope, timestamp
                )

            if self._is_held(aggregate, run, boundary):
                release_preboundary_in_uow(
                    uow, aggregate=aggregate, run=run, now=timestamp
                )
                uow.tasks.save(aggregate.task)
                self._consume(uow, envelope, timestamp)
                uow.commit()
                return CoordinatedDeliveryResult.without_lease(
                    CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
                    reason=self._hold_reason(aggregate, boundary),
                )

            # Admission policy is evaluated after the previous owner has been
            # released, so replacement does not retain stale accounting.
            latest = aggregate.latest_attempts.get(run.id)
            if latest is not None and latest.status is AttemptStatus.RUNNING:
                if latest.lease_expires_at > timestamp:
                    return CoordinatedDeliveryResult.without_lease(
                        CoordinatedDeliveryResultKind.IN_PROGRESS
                    )
                latest.expire(at=timestamp)
                BudgetController.release_attempt(aggregate.task, latest, at=timestamp)
                QuotaController.release_attempt(uow, latest)
                uow.attempts.save(latest)

            rejection = BudgetController.attempt_rejection(
                uow, aggregate.task, now=timestamp
            )
            if rejection is not None:
                return self._wait_for_budget(
                    uow, aggregate, run, envelope, boundary, rejection, timestamp
                )

            if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED:
                self._start_queued_target(uow, aggregate.task, aggregate, run, timestamp)
            elif boundary not in {
                CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
                CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
            }:
                raise RuntimeExecutionConflict("Coordinated delivery boundary is invalid")

            fence = (latest.fencing_token + 1) if latest is not None else 1
            attempt = self._new_attempt(aggregate.task, run, fence, timestamp)
            try:
                # Check quota before mutating Task budget. This keeps a rejected
                # admission side-effect free even in an in-memory UoW.
                if self._feature_gates.is_enabled(Feature.QUOTA_ADMISSION):
                    QuotaController.reserve_attempt(uow, aggregate.task, attempt)
                BudgetController.reserve_attempt(aggregate.task, attempt, at=timestamp)
            except QuotaAdmissionRejected:
                rollback = getattr(uow, "rollback", None)
                if callable(rollback):
                    rollback()
                raise
            uow.attempts.add(attempt)

            execution = self._execution_for(aggregate, run)
            if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
                if execution is None:
                    raise RuntimeExecutionConflict("Prepared delivery lacks Runtime execution")
                snapshot = aggregate.assignment_snapshots_by_execution.get(execution.id)
                if (
                    snapshot is None
                    or snapshot.runtime_execution_id != execution.id
                    or snapshot.assignment_id != execution.assignment_id
                    or snapshot.assignment_digest != execution.assignment_digest
                ):
                    raise RuntimeExecutionConflict(
                        "Prepared delivery Assignment snapshot is incomplete"
                    )
                old_owner = latest.id if latest is not None else None
                old_fence = latest.fencing_token if latest is not None else None
                transferred = execution.claim(
                    attempt_id=attempt.id,
                    fencing_token=fence,
                    expected_owner_attempt_id=old_owner,
                    expected_fencing_token=old_fence,
                    expected_version=execution.version,
                    replacement_authorized=True,
                    now=timestamp,
                )
                uow.runtimes.save_execution(transferred, tenant_id=envelope.tenant_id)

            uow.tasks.save(aggregate.task)
            uow.runs.save(run)
            if run.subtask_id is not None:
                subtask = self._subtask(aggregate, run.subtask_id)
                if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED:
                    uow.subtasks.save(subtask)
            lease = self._lease(
                aggregate.task,
                run,
                attempt,
                uow,
                work_item=prepared_work_item,
            )
            uow.commit()
            result_kind = (
                CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY
                if boundary
                in {
                    CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
                    CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
                }
                else CoordinatedDeliveryResultKind.ACQUIRED
            )
            return (
                CoordinatedDeliveryResult.recovered_pre_boundary(lease)
                if result_kind is CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY
                else CoordinatedDeliveryResult.acquired(lease)
            )

    def _classify_crossed(
        self,
        uow: Any,
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        envelope: MessageEnvelope,
        now: datetime,
    ) -> CoordinatedDeliveryResult:
        attempt = aggregate.latest_attempts.get(run.id)
        execution = self._execution_for(aggregate, run)
        if attempt is None or execution is None:
            raise RuntimeExecutionConflict("Crossed delivery ownership is incomplete")
        if attempt.status is not AttemptStatus.RUNNING:
            raise RuntimeExecutionConflict("Crossed delivery Attempt is not active")
        if attempt.lease_expires_at > now:
            return CoordinatedDeliveryResult.without_lease(
                CoordinatedDeliveryResultKind.IN_PROGRESS
            )
        handle = aggregate.handle_snapshots_by_execution.get(execution.id)
        if handle is not None and (
            handle.runtime_execution_id != execution.id
            or handle.tenant_id != aggregate.task.tenant_id
        ):
            raise RuntimeExecutionConflict("Crossed delivery handle binding is invalid")
        proof = RecoveryCrossedProof(
            execution_id=execution.id,
            expired_owner_attempt_id=attempt.id,
            expired_owner_fencing_token=attempt.fencing_token,
            phase=execution.phase,
            version=execution.version,
            persisted_handle_snapshot_id=handle.id if handle is not None else None,
            persisted_handle_snapshot_digest=handle.handle_digest if handle is not None else None,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
        )
        return CoordinatedDeliveryResult.recover_crossed(proof)

    def _wait_for_budget(
        self,
        uow: Any,
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        envelope: MessageEnvelope,
        boundary: CoordinationRuntimeBoundary,
        reason: str,
        now: datetime,
    ) -> CoordinatedDeliveryResult:
        if aggregate.task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            self._consume(uow, envelope, now)
            uow.commit()
            return CoordinatedDeliveryResult.without_lease(
                CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
                reason="coordination.task_terminal",
            )
        release_preboundary_in_uow(
            uow, aggregate=aggregate, run=run, now=now
        )
        aggregate.task.wait_for_budget(reason, at=now)
        uow.tasks.save(aggregate.task)
        uow.runs.save(run)
        drain = aggregate.active_drain
        if drain is None:
            drain = CoordinationRuntimeDrain.start(
                drain_id=uuid5(
                    NAMESPACE_URL,
                    f"{_DRAIN_NAMESPACE}{aggregate.task.tenant_id}:{aggregate.task.id}",
                ),
                tenant_id=aggregate.task.tenant_id,
                task_id=aggregate.task.id,
                triggering_run_id=run.id,
                target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                reason=reason,
                at=now,
            )
            uow.coordination_runtime_drains.add(drain)
        elif drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL:
            updated = drain.retarget(
                target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                reason=reason,
                at=now,
            )
            if updated is not drain:
                uow.coordination_runtime_drains.save(
                    updated, tenant_id=aggregate.task.tenant_id
                )
        self._consume(uow, envelope, now)
        uow.commit()
        return CoordinatedDeliveryResult.without_lease(
            CoordinatedDeliveryResultKind.WAITING_APPROVAL, reason=reason
        )

    @staticmethod
    def _validate_envelope(
        envelope: MessageEnvelope, *, now: datetime | None
    ) -> tuple[UUID, UUID, datetime]:
        if type(envelope) is not MessageEnvelope:
            raise InvalidMessage("RunRequested envelope is invalid")
        if (
            type(envelope.schema_name) is not str
            or envelope.schema_name != RUN_REQUESTED_SCHEMA
            or type(envelope.schema_version) is not int
            or envelope.schema_version != RUN_REQUESTED_VERSION
            or type(envelope.message_id) is not UUID
            or type(envelope.correlation_id) is not UUID
            or type(envelope.tenant_id) is not str
            or not envelope.tenant_id.strip()
            or envelope.tenant_id != envelope.tenant_id.strip()
            or type(envelope.occurred_at) is not datetime
            or envelope.occurred_at.tzinfo is None
            or envelope.occurred_at.utcoffset() is None
            or type(envelope.producer) is not str
            or not envelope.producer.strip()
            or envelope.producer != envelope.producer.strip()
            or type(envelope.causation_id) not in (UUID, type(None))
            or type(envelope.idempotency_key) is not str
            or not envelope.idempotency_key.strip()
            or envelope.idempotency_key != envelope.idempotency_key.strip()
            or type(envelope.payload) is not dict
            or set(envelope.payload) != {"task_id", "run_id"}
        ):
            raise InvalidMessage("RunRequested envelope is invalid")
        try:
            task_value = envelope.payload["task_id"]
            run_value = envelope.payload["run_id"]
            if (
                type(task_value) is not str
                or type(run_value) is not str
                or str(UUID(task_value)) != task_value
                or str(UUID(run_value)) != run_value
            ):
                raise ValueError("non-canonical UUID")
            task_id = UUID(task_value)
            run_id = UUID(run_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidMessage(
                "RunRequested payload must contain UUID task_id and run_id"
            ) from exc
        if envelope.correlation_id != task_id or envelope.idempotency_key != f"run:{run_id}":
            raise InvalidMessage("RunRequested envelope identity is invalid")
        timestamp = now if now is not None else utc_now()
        if (
            type(timestamp) is not datetime
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise InvalidTaskInput("Coordinated delivery clock must be timezone-aware")
        return task_id, run_id, timestamp.astimezone(timezone.utc)

    @staticmethod
    def _run(aggregate: CoordinatedRuntimeAggregate, run_id: UUID) -> TaskRun:
        values = [run for run in aggregate.runs if run.id == run_id]
        if len(values) != 1:
            raise InvalidMessage("RunRequested references an unknown task run")
        return values[0]

    @staticmethod
    def _subtask(aggregate: CoordinatedRuntimeAggregate, subtask_id: UUID) -> Subtask:
        values = [value for value in aggregate.subtasks if value.id == subtask_id]
        if len(values) != 1:
            raise RuntimeExecutionConflict("Coordinated delivery Subtask binding is invalid")
        return values[0]

    @staticmethod
    def _execution_for(
        aggregate: CoordinatedRuntimeAggregate, run: TaskRun
    ) -> RuntimeExecution | None:
        values = aggregate.executions_by_run.get(run.id, ())
        if run.runtime_execution_id is None:
            return None
        bound = [value for value in values if value.id == run.runtime_execution_id]
        if len(bound) != 1:
            raise RuntimeExecutionConflict("Coordinated delivery Runtime binding is invalid")
        return bound[0]

    @staticmethod
    def _validate_run_binding(
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        task_id: UUID,
        tenant_id: str,
    ) -> None:
        if (
            run.task_id != task_id
            or run.runtime_authority != "managed"
            or run.comparison_mode != "off"
            or run.runtime_version_id not in aggregate.runtime_versions
            or run.runtime_execution_intent_id is None
            or run.agent_version_id is None
            or type(run.agent_version_digest) is not str
            or len(run.agent_version_digest) != 64
            or any(character not in "0123456789abcdef" for character in run.agent_version_digest)
        ):
            raise RuntimeExecutionConflict("Coordinated delivery Run binding is invalid")
        cohort = aggregate.cohort
        if (
            cohort.runtime_version_id != run.runtime_version_id
            or cohort.comparison_mode != run.comparison_mode
        ):
            raise RuntimeExecutionConflict("Coordinated delivery cohort binding is invalid")
        version = aggregate.runtime_versions[run.runtime_version_id]
        try:
            validate_builtin_managed_runtime_version(version)
        except (RuntimeVersionNotFound, InvalidTaskInput) as exc:
            raise RuntimeExecutionConflict(
                "Coordinated delivery Runtime Version is incompatible"
            ) from exc
        if _task_plan_invalid(aggregate.task):
            raise RuntimeExecutionConflict("Coordinated delivery Task plan is invalid")
        if run.role is RunRole.EXECUTOR:
            if run.subtask_id is None:
                raise RuntimeExecutionConflict("Executor delivery Subtask binding is missing")
            subtask = CoordinatedRuntimeDeliveryAcquisitionService._subtask(
                aggregate, run.subtask_id
            )
            if subtask.task_id != task_id or subtask.current_run_id != run.id:
                raise RuntimeExecutionConflict("Executor delivery Subtask binding is invalid")
        elif run.role is RunRole.SUPERVISOR:
            if (
                run.subtask_id is not None
                or aggregate.task.current_run_id != run.id
                or any(
                    value.status not in TERMINAL_SUBTASK_STATUSES
                    for value in aggregate.subtasks
                )
            ):
                raise RuntimeExecutionConflict("Supervisor delivery binding is invalid")
        else:
            raise RuntimeExecutionConflict("Coordinated delivery Run role is invalid")
        if aggregate.task.tenant_id != tenant_id:
            raise RuntimeExecutionConflict("Coordinated delivery tenant binding is invalid")

    @staticmethod
    def _validate_prepared_assignment(
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        execution: Any,
        snapshot: Any,
        tenant_id: str,
    ) -> RuntimeAssignment:
        """Re-validate the immutable assignment chain before ownership transfer."""
        try:
            assignment = parse_assignment_payload(snapshot.canonical_payload)
            validate_runtime_assignment_chain(
                assignment,
                tenant_id=tenant_id,
                task_id=aggregate.task.id,
                run=run,
                execution_id=execution.id,
            )
            version = aggregate.runtime_versions[run.runtime_version_id]
            descriptor_digest = RuntimeDescriptor.from_dict(
                thaw_json(version.descriptor)
            ).digest()
            if (
                assignment.assignment_digest != snapshot.assignment_digest
                or assignment.run_role != run.role.value
                or assignment.revision != run.revision_number
                or assignment.runtime_descriptor_digest != descriptor_digest
                or execution.assignment_id != UUID(assignment.assignment_id)
                or execution.assignment_digest != assignment.assignment_digest
                or execution.runtime_version_id != version.id
            ):
                raise RuntimeExecutionConflict(
                    "Prepared delivery Assignment chain conflicts"
                )
            return assignment
        except (
            InvalidTaskInput,
            RuntimeExecutionConflict,
            RuntimeContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            if isinstance(exc, RuntimeExecutionConflict):
                raise
            raise RuntimeExecutionConflict(
                "Prepared delivery Assignment chain is invalid"
            ) from exc

    @staticmethod
    def _work_item_from_assignment(assignment: RuntimeAssignment) -> WorkflowWorkItem:
        """Recover the exact base work item pinned by a PREPARED Assignment."""
        if type(assignment) is not RuntimeAssignment or type(assignment.objective) is not str:
            raise RuntimeExecutionConflict(
                "Prepared delivery Assignment lacks a recoverable work item"
            )
        if assignment.structured_input is None:
            raise RuntimeExecutionConflict(
                "Prepared delivery Assignment lacks a recoverable work item"
            )
        return WorkflowWorkItem(
            objective=assignment.objective,
            input=dict(assignment.structured_input),
        )

    @staticmethod
    def _is_held(
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        boundary: CoordinationRuntimeBoundary,
    ) -> bool:
        if boundary not in {
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
            CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
            CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
        }:
            return False
        return (
            aggregate.active_drain is not None
            or aggregate.task.status in {
                TaskStatus.PAUSED,
                TaskStatus.PAUSE_REQUESTED,
                TaskStatus.RECONCILIATION_REQUIRED,
                TaskStatus.WAITING_APPROVAL,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
            }
            or run.status
            in {
                RunStatus.PAUSED,
                RunStatus.PAUSE_REQUESTED,
                RunStatus.RECONCILIATION_REQUIRED,
            }
        )

    @staticmethod
    def _hold_reason(
        aggregate: CoordinatedRuntimeAggregate,
        boundary: CoordinationRuntimeBoundary,
    ) -> str:
        if aggregate.active_drain is not None:
            return aggregate.active_drain.reason
        if aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED:
            return "coordination.runtime_reconciliation_required"
        return "coordination.runtime_delivery_blocked"

    @staticmethod
    def _start_queued_target(
        uow: Any,
        task: Task,
        aggregate: CoordinatedRuntimeAggregate,
        run: TaskRun,
        now: datetime,
    ) -> None:
        if run.role is RunRole.EXECUTOR:
            subtask = CoordinatedRuntimeDeliveryAcquisitionService._subtask(
                aggregate, run.subtask_id
            )
            if subtask.status is not SubtaskStatus.READY or task.status is not TaskStatus.RUNNING:
                raise InvalidTaskTransition("Queued Executor delivery is not ready")
            subtask.start(run.id, at=now)
            uow.subtasks.save(subtask)
        elif run.role is RunRole.SUPERVISOR:
            if task.status is not TaskStatus.RUNNING or task.current_run_id != run.id:
                raise InvalidTaskTransition("Queued Supervisor delivery is not ready")
        run.start(at=now)

    def _new_attempt(
        self, task: Task, run: TaskRun, fence: int, now: datetime
    ) -> TaskAttempt:
        policy = task.budget
        return TaskAttempt.lease(
            run_id=run.id,
            worker_id=self._worker_id,
            fencing_token=fence,
            lease_expires_at=now + self._lease_duration,
            reserved_tokens=policy.token_reservation_per_attempt if policy else 0,
            reserved_cost_micros=policy.cost_reservation_micros_per_attempt if policy else 0,
            at=now,
        )

    def _lease(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        uow: Any,
        *,
        work_item: WorkflowWorkItem | None = None,
    ) -> CoordinatedDeliveryLeaseV1:
        work_item = work_item or self._work_item_builder.build(task, run, uow=uow)
        stable = assignment_projection_digest(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            subtask_id=run.subtask_id,
            role=run.role,
            runtime_version_id=run.runtime_version_id,
            runtime_execution_intent_id=run.runtime_execution_intent_id,
            agent_version_id=run.agent_version_id,
            agent_version_digest=run.agent_version_digest,
            task_plan_version=task.plan_version,
            task_plan_digest=task.plan_digest,
            run_revision=run.revision_number,
            work_item=work_item,
        )
        own = ownership_digest(
            assignment_projection_digest=stable,
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            subtask_id=run.subtask_id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            lease_token=attempt.lease_token,
            lease_deadline=attempt.lease_expires_at,
        )
        return CoordinatedDeliveryLeaseV1(
            schema_version=1,
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            subtask_id=run.subtask_id,
            attempt_id=attempt.id,
            role=run.role,
            fencing_token=attempt.fencing_token,
            lease_token=attempt.lease_token,
            lease_deadline=attempt.lease_expires_at,
            runtime_version_id=run.runtime_version_id,
            runtime_execution_intent_id=run.runtime_execution_intent_id,
            task_plan_version=task.plan_version,
            task_plan_digest=task.plan_digest,
            run_revision=run.revision_number,
            agent_version_id=run.agent_version_id,
            agent_version_digest=run.agent_version_digest,
            work_item=work_item,
            assignment_projection_digest=stable,
            ownership_digest=own,
        )

    def _consume(
        self, uow: Any, envelope: MessageEnvelope, now: datetime
    ) -> None:
        uow.inbox.add(
            InboxMessage.processed(self._consumer_name, envelope, at=now)
        )

    @staticmethod
    def _validate_cohort(
        aggregate: CoordinatedRuntimeAggregate,
        task_id: UUID,
        tenant_id: str,
    ) -> None:
        """Recheck the persisted cohort identity at the delivery boundary."""
        cohort = aggregate.cohort
        if (
            cohort.task_id != task_id
            or cohort.tenant_id != tenant_id
            or cohort.runtime_authority not in {"legacy", "managed"}
            or cohort.comparison_mode not in {"off", "deterministic_shadow"}
        ):
            raise RuntimeExecutionConflict("Coordinated delivery cohort is invalid")
        runs = aggregate.runs
        if not runs:
            return
        authorities = {run.runtime_authority for run in runs}
        comparisons = {run.comparison_mode for run in runs}
        if len(authorities) != 1 or len(comparisons) != 1:
            raise RuntimeExecutionConflict("Coordinated delivery cohort is mixed")
        if (
            cohort.runtime_authority != next(iter(authorities))
            or cohort.comparison_mode != next(iter(comparisons))
        ):
            raise RuntimeExecutionConflict("Coordinated delivery cohort is stale")
        version_ids = {run.runtime_version_id for run in runs}
        if next(iter(authorities)) == "managed":
            if (
                len(version_ids) != 1
                or None in version_ids
                or cohort.runtime_version_id != next(iter(version_ids))
            ):
                raise RuntimeExecutionConflict("Coordinated delivery Runtime cohort is invalid")

def _task_plan_invalid(task: Task) -> bool:
    """Keep the lease contract's plan fields closed and canonical."""
    return (
        type(task.plan_version) is not int
        or task.plan_version < 1
        or type(task.plan_digest) is not str
        or _PLAN_DIGEST.fullmatch(task.plan_digest) is None
    )


__all__ = ["CoordinatedRuntimeDeliveryAcquisitionService"]
