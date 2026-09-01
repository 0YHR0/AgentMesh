"""The transaction-local business outcome policy.

This module deliberately owns only Task/Run/Attempt (and the small coordinated
Subtask projection).  Runtime evidence, accounting, quota, inbox and commit
remain the responsibility of the caller that owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from agentmesh.application.agent_resolution import resolve_default_agent
from agentmesh.application.authority_cohorts import (
    AuthorityCohortResolver,
    ContinuationKind,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.domain.budgets import BudgetSettlementSource
from agentmesh.domain.coordination import SubtaskStatus
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import (
    AttemptStatus,
    ReviewDecision,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)
from agentmesh.features import FeatureGateSet


class ProgressionContext(str, Enum):
    ORDINARY = "ORDINARY"
    DIRECT_RECONCILIATION = "DIRECT_RECONCILIATION"


class KnownTerminalPhase(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"


class AccountingDisposition(str, Enum):
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    ALREADY_CONSERVATIVE = "ALREADY_CONSERVATIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class BusinessOutcomeApplication:
    """Immutable result of applying one known terminal business outcome."""

    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    task_status: TaskStatus
    run_status: RunStatus
    attempt_status: AttemptStatus
    new_run_ids: tuple[UUID, ...]
    task_completed: bool
    may_capture_completion_memory: bool
    accounting_disposition: AccountingDisposition
    progression_context: ProgressionContext
    reconciliation_action: TaskResolutionAction | None = None
    reconciliation_reason: str | None = None

    @property
    def completion_memory_allowed(self) -> bool:
        """Compatibility/readability alias for callers producing Memory."""
        return self.may_capture_completion_memory

    @classmethod
    def from_entities(
        cls,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        *,
        pre_task_status: TaskStatus,
        new_runs: tuple[TaskRun, ...] = (),
        accounting_disposition: AccountingDisposition,
        progression_context: ProgressionContext,
        reconciliation_action: TaskResolutionAction | None = None,
        reconciliation_reason: str | None = None,
    ) -> BusinessOutcomeApplication:
        """Build a summary from the exact post-mutation entities."""
        completed = (
            pre_task_status is not TaskStatus.COMPLETED and task.status is TaskStatus.COMPLETED
        )
        return cls(
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            task_status=task.status,
            run_status=run.status,
            attempt_status=attempt.status,
            new_run_ids=tuple(item.id for item in new_runs),
            task_completed=completed,
            may_capture_completion_memory=completed,
            accounting_disposition=accounting_disposition,
            progression_context=progression_context,
            reconciliation_action=reconciliation_action,
            reconciliation_reason=reconciliation_reason,
        )

    def __post_init__(self) -> None:
        if not all(type(value) is UUID for value in (self.task_id, self.run_id, self.attempt_id)):
            raise InvalidTaskInput("Business outcome summary identities are invalid")
        if any(type(value) is not UUID for value in self.new_run_ids):
            raise InvalidTaskInput("Business outcome continuation identity is invalid")
        if self.task_completed != (self.task_status is TaskStatus.COMPLETED):
            raise InvalidTaskInput("Business outcome completion summary is inconsistent")
        if self.may_capture_completion_memory != self.task_completed:
            raise InvalidTaskInput("Completion Memory is allowed only for completed Tasks")
        if self.reconciliation_action is not None and not isinstance(
            self.reconciliation_action, TaskResolutionAction
        ):
            raise InvalidTaskInput("Reconciliation action is invalid")
        if (self.reconciliation_action is None) != (self.reconciliation_reason is None):
            raise InvalidTaskInput("Reconciliation action and reason must be paired")
        if self.progression_context is ProgressionContext.DIRECT_RECONCILIATION:
            if self.reconciliation_action is None or self.reconciliation_reason is None:
                raise InvalidTaskInput("Reconciliation summary requires action and reason")
        elif self.reconciliation_action is not None or self.reconciliation_reason is not None:
            raise InvalidTaskInput("Ordinary summary cannot contain reconciliation details")
        if self.reconciliation_reason is not None and (
            len(self.reconciliation_reason) > 512
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.reconciliation_reason
            )
        ):
            raise InvalidTaskInput("Reconciliation reason is unsafe")


class BusinessOutcomeApplier:
    """Apply the closed A4.2a.1 Task outcome matrix in a caller UoW."""

    def __init__(
        self,
        *,
        authority_cohort_resolver: AuthorityCohortResolver | None = None,
        executor_agent_id: str = "demo-agent",
        reviewer_agent_id: str = "demo-reviewer",
        coordinated_scheduler: CoordinatedScheduler | None = None,
    ) -> None:
        self._resolver = authority_cohort_resolver or AuthorityCohortResolver(
            feature_gates=FeatureGateSet.from_config("minimal")
        )
        self._executor_agent_id = executor_agent_id
        self._reviewer_agent_id = reviewer_agent_id
        self._coordinated_scheduler = coordinated_scheduler

    def apply_known_terminal_in_uow(
        self,
        uow: Any,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        progression_context: ProgressionContext,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        budget_rejection: str | None,
        cancel_intent_present: bool,
        accounting_disposition: AccountingDisposition,
        finalized_at: datetime,
        causation_id: UUID,
    ) -> BusinessOutcomeApplication:
        """Validate, mutate and save one business outcome without committing."""
        context = self._as_enum(progression_context, ProgressionContext, "progression context")
        terminal_phase = self._as_enum(phase, KnownTerminalPhase, "terminal phase")
        disposition = self._as_enum(
            accounting_disposition, AccountingDisposition, "accounting disposition"
        )
        at = self._normalize_at(finalized_at)
        self._validate_arguments(
            task,
            run,
            attempt,
            terminal_phase,
            output,
            safe_error,
            budget_rejection,
            cancel_intent_present,
            disposition,
            causation_id,
        )

        locked_task = uow.tasks.get(task.id, for_update=True)
        locked_run = uow.runs.get(run.id, for_update=True)
        locked_attempt = uow.attempts.get(attempt.id, for_update=True)
        latest_attempt = uow.attempts.latest_for_run(run.id, for_update=True)
        self._validate_chain(
            task, run, attempt, locked_task, locked_run, locked_attempt, latest_attempt
        )
        # The domain policy clock is checked before any accounting or business mutation.
        locked_task.validate_policy_at(at)
        self._validate_accounting(locked_task, locked_attempt, disposition, context, terminal_phase)
        pre_task_status = locked_task.status

        managed = locked_run.runtime_authority == "managed"
        if context is ProgressionContext.DIRECT_RECONCILIATION:
            self._validate_reconciliation(locked_task, locked_run, locked_attempt, managed)
            return self._apply_reconciliation(
                uow,
                locked_task,
                locked_run,
                locked_attempt,
                terminal_phase,
                output,
                safe_error,
                budget_rejection,
                disposition,
                at,
                cancel_intent_present,
                pre_task_status,
            )

        if managed and locked_task.execution_mode in {
            TaskExecutionMode.REVIEWED,
            TaskExecutionMode.COORDINATED,
        }:
            raise InvalidTaskTransition(
                "Managed REVIEWED and COORDINATED outcomes are not enabled in A4.2a.1"
            )
        effective_phase = terminal_phase
        if managed and terminal_phase is KnownTerminalPhase.CANCELED and not cancel_intent_present:
            effective_phase = KnownTerminalPhase.FAILED
            safe_error = "runtime.unrequested_cancellation"

        pause_alignment = (
            managed
            and locked_task.status is TaskStatus.PAUSE_REQUESTED
            and locked_run.status is RunStatus.PAUSE_REQUESTED
            and locked_attempt.status is AttemptStatus.RUNNING
        )
        if (
            managed
            and any(
                value in {TaskStatus.PAUSE_REQUESTED, RunStatus.PAUSE_REQUESTED}
                for value in (locked_task.status, locked_run.status)
            )
            and not pause_alignment
        ):
            raise InvalidTaskTransition("Managed pause outcome requires exact aligned states")

        self._validate_ordinary_activation(uow, locked_task, locked_run, locked_attempt)

        new_runs: list[TaskRun] = []
        decision: ReviewDecision | None = None
        if (
            locked_task.execution_mode is TaskExecutionMode.REVIEWED
            and effective_phase is KnownTerminalPhase.SUCCEEDED
        ):
            if locked_run.role is RunRole.REVIEWER:
                if output is None:
                    raise InvalidTaskInput("Reviewer success requires an output object")
                decision = ReviewDecision.from_output(output, locked_task.acceptance_criteria)
                if (
                    not decision.accepted
                    and budget_rejection is None
                    and self._can_revision(locked_task, at)
                ):
                    new_runs.append(
                        self._make_continuation(
                            uow,
                            locked_task,
                            locked_run,
                            agent_id=self._executor_agent_id,
                            role=RunRole.EXECUTOR,
                            revision_number=locked_task.revision_count + 1,
                            kind=ContinuationKind.REVISION,
                            at=at,
                        )
                    )
            elif locked_run.role is not RunRole.EXECUTOR:
                raise InvalidTaskTransition("Reviewed outcome has an invalid Run role")
            elif budget_rejection is None:
                new_runs.append(
                    self._make_continuation(
                        uow,
                        locked_task,
                        locked_run,
                        agent_id=self._reviewer_agent_id,
                        role=RunRole.REVIEWER,
                        kind=ContinuationKind.REVIEWER,
                        at=at,
                    )
                )

        if pause_alignment:
            locked_attempt.finalize_managed_after_pause_request(
                effective_phase.value, safe_error=safe_error, at=at
            )
            locked_run.finalize_managed_after_pause_request(
                effective_phase.value, output=output, safe_error=safe_error, at=at
            )
            locked_task.finalize_managed_after_pause_request(
                locked_run.id,
                effective_phase.value,
                output=output,
                safe_error=safe_error,
                budget_rejection=budget_rejection,
                at=at,
            )
        elif locked_task.execution_mode is TaskExecutionMode.COORDINATED:
            return self._apply_legacy_coordinated(
                uow,
                locked_task,
                locked_run,
                locked_attempt,
                effective_phase,
                output,
                safe_error,
                disposition,
                causation_id,
                at,
                pre_task_status,
            )
        else:
            self._apply_run_and_attempt(
                locked_task,
                locked_run,
                locked_attempt,
                effective_phase,
                output,
                safe_error,
                at,
            )
            self._apply_task(
                locked_task,
                locked_run,
                effective_phase,
                output,
                safe_error,
                budget_rejection,
                decision,
                new_runs,
                at,
            )

        uow.tasks.save(locked_task)
        uow.runs.save(locked_run)
        uow.attempts.save(locked_attempt)
        self._persist_continuations(uow, locked_task, locked_run, new_runs, causation_id, at)
        return self._summary(
            locked_task,
            locked_run,
            locked_attempt,
            new_runs,
            disposition,
            context,
            None,
            None,
            pre_task_status,
        )

    def _apply_legacy_coordinated(
        self,
        uow: Any,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        disposition: AccountingDisposition,
        causation_id: UUID,
        at: datetime,
        pre_task_status: TaskStatus,
    ) -> BusinessOutcomeApplication:
        subtask = None
        if run.role is RunRole.EXECUTOR:
            if run.subtask_id is None:
                raise InvalidTaskTransition("Coordinated executor Run has no Subtask binding")
            subtask = uow.subtasks.get(run.subtask_id, for_update=True)
            if subtask is None or subtask.task_id != task.id:
                raise InvalidTaskTransition("Coordinated Run references an unknown Subtask")
            if subtask.status is not SubtaskStatus.RUNNING:
                raise InvalidTaskTransition("Coordinated Subtask is not running")
        elif run.role is not RunRole.SUPERVISOR or run.subtask_id is not None:
            raise InvalidTaskTransition("Coordinated Run role/binding is invalid")

        self._apply_run_and_attempt(task, run, attempt, phase, output, safe_error, at)
        new_runs: list[TaskRun] = []
        if run.role is RunRole.SUPERVISOR:
            if phase is KnownTerminalPhase.SUCCEEDED:
                assert output is not None
                task.complete(run.id, output, at=at)
            elif phase is KnownTerminalPhase.CANCELED:
                task.cancel(at=at)
            else:
                task.fail_coordination(
                    safe_error
                    or (
                        "runtime.timed_out"
                        if phase is KnownTerminalPhase.TIMED_OUT
                        else "runtime.failed"
                    ),
                    at=at,
                )
        else:
            assert subtask is not None
            if phase is KnownTerminalPhase.SUCCEEDED:
                assert output is not None
                subtask.complete(run.id, output, at=at)
            elif phase is KnownTerminalPhase.CANCELED:
                subtask.cancel(at=at)
            else:
                subtask.fail(
                    safe_error
                    or (
                        "runtime.timed_out"
                        if phase is KnownTerminalPhase.TIMED_OUT
                        else "runtime.failed"
                    ),
                    at=at,
                )
            uow.subtasks.save(subtask)
            if phase is KnownTerminalPhase.SUCCEEDED and self._coordinated_scheduler is not None:
                new_runs = self._coordinated_scheduler.schedule(
                    uow, task, at=at, causation_id=causation_id
                )

        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        # CoordinatedScheduler owns continuation persistence and messages.
        return self._summary(
            task,
            run,
            attempt,
            new_runs,
            disposition,
            ProgressionContext.ORDINARY,
            None,
            None,
            pre_task_status,
        )

    @staticmethod
    def _as_enum(value: Any, enum_type: type[Enum], label: str) -> Any:
        try:
            return (
                value if isinstance(value, enum_type) else enum_type(getattr(value, "value", value))
            )
        except (TypeError, ValueError) as exc:
            raise InvalidTaskInput(f"Unknown {label}") from exc

    @staticmethod
    def _normalize_at(value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTaskInput("Policy transition time must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_arguments(
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        budget_rejection: str | None,
        cancel_intent_present: bool,
        disposition: AccountingDisposition,
        causation_id: UUID,
    ) -> None:
        if type(causation_id) is not UUID:
            raise InvalidTaskInput("Outcome causation ID is invalid")
        if type(cancel_intent_present) is not bool:
            raise InvalidTaskInput("Cancellation intent flag is invalid")
        if cancel_intent_present and (
            run.runtime_authority != "managed" or phase is not KnownTerminalPhase.CANCELED
        ):
            raise InvalidTaskInput("Cancellation intent only applies to managed cancellation")
        if phase is KnownTerminalPhase.SUCCEEDED and type(output) is not dict:
            raise InvalidTaskInput("Successful outcome requires an output object")
        if phase is KnownTerminalPhase.SUCCEEDED and safe_error is not None:
            raise InvalidTaskInput("Successful outcome cannot carry a safe error")
        if phase is not KnownTerminalPhase.SUCCEEDED and output is not None:
            raise InvalidTaskInput("Non-success outcome cannot carry output")
        if safe_error is not None and (not isinstance(safe_error, str) or not safe_error.strip()):
            raise InvalidTaskInput("Safe outcome error must not be empty")
        if safe_error is not None and len(safe_error.strip()) > 512:
            raise InvalidTaskInput("Safe outcome error is too long")
        if safe_error is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in safe_error
        ):
            raise InvalidTaskInput("Safe outcome error must not contain control characters")
        if budget_rejection is not None and (
            not isinstance(budget_rejection, str) or not budget_rejection.strip()
        ):
            raise InvalidTaskInput("Budget rejection must not be empty")
        if budget_rejection is not None and phase is not KnownTerminalPhase.SUCCEEDED:
            raise InvalidTaskInput("Budget rejection only applies to successful outcomes")
        if budget_rejection is not None and budget_rejection.strip() != "budget_deadline_exceeded":
            raise InvalidTaskInput("Budget rejection reason is not supported")
        if type(task.id) is not UUID or type(run.id) is not UUID or type(attempt.id) is not UUID:
            raise InvalidTaskInput("Outcome entity identity is invalid")
        if disposition is AccountingDisposition.ALREADY_CONSERVATIVE and phase not in {
            KnownTerminalPhase.SUCCEEDED,
            KnownTerminalPhase.FAILED,
            KnownTerminalPhase.CANCELED,
            KnownTerminalPhase.TIMED_OUT,
        }:
            raise InvalidTaskInput("Conservative accounting requires a known terminal phase")

    @staticmethod
    def _validate_chain(
        supplied_task: Task,
        supplied_run: TaskRun,
        supplied_attempt: TaskAttempt,
        task: Task | None,
        run: TaskRun | None,
        attempt: TaskAttempt | None,
        latest: TaskAttempt | None,
    ) -> None:
        if task is None or run is None or attempt is None or latest is None:
            raise InvalidTaskTransition("Outcome chain could not be locked")
        if task.id != supplied_task.id or task.tenant_id != supplied_task.tenant_id:
            raise InvalidTaskTransition("Task changed while applying outcome")
        if run.id != supplied_run.id or run.task_id != task.id or supplied_run.task_id != task.id:
            raise InvalidTaskTransition("Run is not owned by Task")
        if attempt.id != supplied_attempt.id or attempt.run_id != run.id:
            raise InvalidTaskTransition("Attempt is not owned by Run")
        if latest.id != attempt.id:
            raise InvalidTaskTransition("Outcome Attempt is not the latest Attempt")
        if (
            supplied_attempt.worker_id != attempt.worker_id
            or supplied_attempt.lease_token != attempt.lease_token
            or supplied_attempt.fencing_token != attempt.fencing_token
        ):
            raise InvalidTaskTransition("Outcome Attempt owner or fence changed")

    @staticmethod
    def _validate_accounting(
        task: Task,
        attempt: TaskAttempt,
        disposition: AccountingDisposition,
        context: ProgressionContext,
        phase: KnownTerminalPhase,
    ) -> None:
        source = attempt.budget_settlement_source
        if context is ProgressionContext.DIRECT_RECONCILIATION:
            if disposition not in {
                AccountingDisposition.ALREADY_CONSERVATIVE,
                AccountingDisposition.NOT_APPLICABLE,
            }:
                raise InvalidTaskTransition("Reconciliation cannot settle or release accounting")
            if disposition is AccountingDisposition.ALREADY_CONSERVATIVE and (
                task.budget is None or source is not BudgetSettlementSource.CONSERVATIVE_ESTIMATE
            ):
                raise InvalidTaskTransition("Reconciliation requires conservative accounting")
            if disposition is AccountingDisposition.NOT_APPLICABLE and (
                task.budget is not None or source is not None
            ):
                raise InvalidTaskTransition("No-budget reconciliation accounting is mismatched")
            return
        if task.budget is None:
            if disposition is not AccountingDisposition.NOT_APPLICABLE or source is not None:
                raise InvalidTaskTransition("No-budget accounting disposition is mismatched")
            return
        expected = (
            AccountingDisposition.SETTLED
            if phase is KnownTerminalPhase.SUCCEEDED
            else AccountingDisposition.RELEASED
        )
        if disposition is not expected:
            raise InvalidTaskTransition("Ordinary accounting disposition is mismatched")
        if disposition is AccountingDisposition.NOT_APPLICABLE:
            if task.budget is not None or source is not None:
                raise InvalidTaskTransition("No-budget accounting disposition is mismatched")
        elif disposition is AccountingDisposition.SETTLED:
            if task.budget is None or source not in {
                BudgetSettlementSource.ACTUAL,
                BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
            }:
                raise InvalidTaskTransition("Settled accounting disposition is mismatched")
        elif disposition is AccountingDisposition.RELEASED:
            if task.budget is None or source is not BudgetSettlementSource.RELEASED:
                raise InvalidTaskTransition("Released accounting disposition is mismatched")
        elif disposition is AccountingDisposition.ALREADY_CONSERVATIVE:
            raise InvalidTaskTransition("Conservative accounting is only valid for reconciliation")

    @staticmethod
    def _validate_reconciliation(
        task: Task, run: TaskRun, attempt: TaskAttempt, managed: bool
    ) -> None:
        if not managed or task.execution_mode is not TaskExecutionMode.DIRECT:
            raise InvalidTaskTransition("Only managed DIRECT reconciliation is enabled")
        if (
            task.status is not TaskStatus.RECONCILIATION_REQUIRED
            or run.status is not RunStatus.RECONCILIATION_REQUIRED
            or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
            or run.role is not RunRole.EXECUTOR
            or task.current_run_id != run.id
        ):
            raise InvalidTaskTransition("Managed direct reconciliation pre-state is invalid")

    @staticmethod
    def _validate_ordinary_activation(
        uow: Any,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
    ) -> None:
        """Validate the closed ordinary matrix before planning continuations."""
        if attempt.status is not AttemptStatus.RUNNING:
            raise InvalidTaskTransition("Outcome Attempt is not running")
        if run.status is not RunStatus.RUNNING:
            if not (
                run.runtime_authority == "managed"
                and task.status is TaskStatus.PAUSE_REQUESTED
                and run.status is RunStatus.PAUSE_REQUESTED
            ):
                raise InvalidTaskTransition("Outcome Run is not in an active state")
        if task.execution_mode is TaskExecutionMode.DIRECT:
            if task.current_run_id != run.id:
                raise InvalidTaskTransition("Outcome Run is not the Task current Run")
            if run.role is not RunRole.EXECUTOR or run.subtask_id is not None:
                raise InvalidTaskTransition("DIRECT outcome role or binding is invalid")
            if task.status not in {TaskStatus.RUNNING, TaskStatus.PAUSE_REQUESTED}:
                raise InvalidTaskTransition("DIRECT Task is not in an active state")
            if run.runtime_authority == "legacy" and task.status is TaskStatus.PAUSE_REQUESTED:
                raise InvalidTaskTransition("Legacy pause-request outcome is unsupported")
            return
        if task.execution_mode is TaskExecutionMode.REVIEWED:
            if task.current_run_id != run.id:
                raise InvalidTaskTransition("Outcome Run is not the Task current Run")
            if run.subtask_id is not None or run.role not in {RunRole.EXECUTOR, RunRole.REVIEWER}:
                raise InvalidTaskTransition("REVIEWED outcome role or binding is invalid")
            expected = TaskStatus.REVIEWING if run.role is RunRole.REVIEWER else TaskStatus.RUNNING
            if task.status is not expected:
                raise InvalidTaskTransition("REVIEWED Task and Run role state do not match")
            return
        if task.execution_mode is TaskExecutionMode.COORDINATED:
            if run.runtime_authority == "managed":
                raise InvalidTaskTransition(
                    "Managed coordinated outcomes require the drain barrier"
                )
            if task.status is not TaskStatus.RUNNING:
                raise InvalidTaskTransition("COORDINATED Task is not running")
            if run.role is RunRole.SUPERVISOR:
                if run.subtask_id is not None:
                    raise InvalidTaskTransition("Supervisor Run cannot bind a Subtask")
                if task.current_run_id != run.id:
                    raise InvalidTaskTransition("Supervisor Run is not the Task current Run")
                return
            if run.role is not RunRole.EXECUTOR or run.subtask_id is None:
                raise InvalidTaskTransition("Coordinated executor role or binding is invalid")
            subtask = uow.subtasks.get(run.subtask_id, for_update=True)
            if subtask is None or subtask.task_id != task.id:
                raise InvalidTaskTransition("Coordinated Subtask binding is invalid")
            if subtask.status is not SubtaskStatus.RUNNING or subtask.current_run_id != run.id:
                raise InvalidTaskTransition("Coordinated Subtask is not active for Run")
            return
        raise InvalidTaskTransition("Unsupported Task execution mode")

    def _apply_reconciliation(
        self,
        uow: Any,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        budget_rejection: str | None,
        disposition: AccountingDisposition,
        at: datetime,
        cancel_intent_present: bool,
        pre_task_status: TaskStatus,
    ) -> BusinessOutcomeApplication:
        if phase is KnownTerminalPhase.SUCCEEDED:
            assert output is not None
            task.reconcile_runtime_succeeded(
                run.id,
                output,
                budget_deadline_exceeded=budget_rejection is not None,
                at=at,
            )
            run.reconcile_runtime_succeeded(output, at=at)
            attempt.reconcile_runtime_succeeded(at=at)
            action = TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED
            reason = (
                "budget_deadline_exceeded"
                if budget_rejection is not None
                else "runtime.confirmed_success"
            )
        elif phase is KnownTerminalPhase.CANCELED:
            if cancel_intent_present:
                reason = "runtime.reconciled_canceled"
                task.reconcile_runtime_canceled(run.id, reason, at=at)
                run.reconcile_runtime_canceled(reason, at=at)
                attempt.reconcile_runtime_canceled(reason, at=at)
            else:
                reason = "runtime.unrequested_cancellation"
                task.reconcile_runtime_failed(run.id, reason, at=at)
                run.reconcile_runtime_failed(reason, at=at)
                attempt.reconcile_runtime_failed(reason, at=at)
            action = TaskResolutionAction.RECONCILE_RUNTIME_CANCELED
        else:
            reason = (
                "runtime.reconciled_timed_out"
                if phase is KnownTerminalPhase.TIMED_OUT
                else "runtime.reconciled_failed"
            )
            task.reconcile_runtime_failed(run.id, reason, at=at)
            run.reconcile_runtime_failed(reason, at=at)
            attempt.reconcile_runtime_failed(reason, at=at)
            action = (
                TaskResolutionAction.RECONCILE_RUNTIME_TIMED_OUT
                if phase is KnownTerminalPhase.TIMED_OUT
                else TaskResolutionAction.RECONCILE_RUNTIME_FAILED
            )
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        return self._summary(
            task,
            run,
            attempt,
            [],
            disposition,
            ProgressionContext.DIRECT_RECONCILIATION,
            action,
            reason,
            pre_task_status,
        )

    @staticmethod
    def _apply_run_and_attempt(
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        at: datetime,
    ) -> None:
        if phase is KnownTerminalPhase.SUCCEEDED:
            assert output is not None
            run.succeed(output, at=at)
            attempt.succeed(at=at)
        elif phase is KnownTerminalPhase.CANCELED:
            run.cancel(at=at)
            attempt.cancel(at=at)
        else:
            reason = safe_error or (
                "runtime.timed_out" if phase is KnownTerminalPhase.TIMED_OUT else "runtime.failed"
            )
            run.fail(reason, at=at)
            attempt.fail(reason, at=at)

    def _apply_task(
        self,
        task: Task,
        run: TaskRun,
        phase: KnownTerminalPhase,
        output: dict[str, Any] | None,
        safe_error: str | None,
        budget_rejection: str | None,
        decision: ReviewDecision | None,
        new_runs: list[TaskRun],
        at: datetime,
    ) -> None:
        if task.execution_mode is TaskExecutionMode.DIRECT:
            if phase is KnownTerminalPhase.SUCCEEDED:
                assert output is not None
                if budget_rejection is not None:
                    task.wait_for_budget(budget_rejection, candidate_output=output, at=at)
                else:
                    task.complete(run.id, output, at=at)
            elif phase is KnownTerminalPhase.CANCELED:
                task.cancel(at=at)
            else:
                task.fail(
                    run.id,
                    safe_error
                    or (
                        "runtime.timed_out"
                        if phase is KnownTerminalPhase.TIMED_OUT
                        else "runtime.failed"
                    ),
                    at=at,
                )
            return
        if task.execution_mode is TaskExecutionMode.REVIEWED:
            if phase is not KnownTerminalPhase.SUCCEEDED:
                if phase is KnownTerminalPhase.CANCELED:
                    task.cancel(at=at)
                else:
                    task.fail(
                        run.id,
                        safe_error
                        or (
                            "runtime.timed_out"
                            if phase is KnownTerminalPhase.TIMED_OUT
                            else "runtime.failed"
                        ),
                        at=at,
                    )
                return
            if run.role is RunRole.EXECUTOR:
                assert output is not None
                if budget_rejection is not None:
                    task.wait_for_budget(budget_rejection, candidate_output=output, at=at)
                else:
                    task.queue_review(run.id, output, new_runs[0].id, at=at)
            else:
                assert decision is not None
                if budget_rejection is not None:
                    task.latest_review = decision.to_dict()
                    task.wait_for_budget(
                        budget_rejection,
                        candidate_output=task.candidate_output,
                        at=at,
                    )
                else:
                    revision_id = new_runs[0].id if new_runs else None
                    task.apply_review(run.id, decision, revision_id, evaluated_at=at, at=at)
            return
        if task.execution_mode is TaskExecutionMode.COORDINATED:
            raise InvalidTaskTransition("Coordinated Task requires local coordination handling")
        raise InvalidTaskTransition("Unsupported Task execution mode")

    def _make_continuation(
        self,
        uow: Any,
        task: Task,
        parent_run: TaskRun,
        *,
        agent_id: str,
        role: RunRole,
        kind: ContinuationKind,
        revision_number: int = 0,
        at: datetime,
    ) -> TaskRun:
        name, version = resolve_default_agent(uow, task.tenant_id, agent_id)
        return self._resolver.create_continuation_in_uow(
            uow,
            task,
            agent_id=name,
            agent_version_id=version.id,
            agent_version_digest=version.content_digest,
            role=role,
            revision_number=revision_number,
            parent_run=parent_run,
            kind=kind,
            at=at,
        )

    @staticmethod
    def _can_revision(task: Task, at: datetime) -> bool:
        return (
            not (task.review_deadline is not None and at >= task.review_deadline)
            and task.revision_count < task.max_revisions
        )

    @staticmethod
    def _persist_continuations(
        uow: Any,
        task: Task,
        parent_run: TaskRun,
        runs: list[TaskRun],
        causation_id: UUID,
        at: datetime,
    ) -> None:
        for run in runs:
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

    @staticmethod
    def _summary(
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        new_runs: list[TaskRun],
        disposition: AccountingDisposition,
        context: ProgressionContext,
        action: TaskResolutionAction | None,
        reason: str | None,
        pre_task_status: TaskStatus,
    ) -> BusinessOutcomeApplication:
        return BusinessOutcomeApplication.from_entities(
            task,
            run,
            attempt,
            pre_task_status=pre_task_status,
            new_runs=tuple(new_runs),
            accounting_disposition=disposition,
            progression_context=context,
            reconciliation_action=action,
            reconciliation_reason=reason,
        )


__all__ = [
    "AccountingDisposition",
    "BusinessOutcomeApplication",
    "BusinessOutcomeApplier",
    "KnownTerminalPhase",
    "ProgressionContext",
]
