"""The transaction-local business outcome policy.

This module deliberately owns only Task/Run/Attempt (and the small coordinated
Subtask projection).  Runtime evidence, accounting, quota, inbox and commit
remain the responsibility of the caller that owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
class PreparedAccountingTransition:
    """Typed before/after proof for a caller-owned budget transition."""

    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    attempt_reserved_tokens: int
    attempt_reserved_cost_micros: int
    attempt_worker_id: str
    attempt_fencing_token: int
    attempt_lease_token: UUID
    before_task_settled_tokens: int
    before_task_reserved_tokens: int
    before_task_settled_cost_micros: int
    before_task_reserved_cost_micros: int
    before_task_budget_revision: int
    before_task_version: int
    before_task_updated_at: datetime
    after_task_settled_tokens: int
    after_task_reserved_tokens: int
    after_task_settled_cost_micros: int
    after_task_reserved_cost_micros: int
    after_task_budget_revision: int
    after_task_version: int
    after_task_updated_at: datetime
    before_attempt_settled_tokens: int | None
    before_attempt_settled_cost_micros: int | None
    before_attempt_source: BudgetSettlementSource | None
    after_attempt_settled_tokens: int | None
    after_attempt_settled_cost_micros: int | None
    after_attempt_source: BudgetSettlementSource | None
    finalized_at: datetime

    @classmethod
    def from_entities(
        cls,
        before_task: Task,
        before_attempt: TaskAttempt,
        after_task: Task,
        after_attempt: TaskAttempt,
        *,
        run_id: UUID,
        finalized_at: datetime,
    ) -> PreparedAccountingTransition:
        if type(finalized_at) is not datetime or finalized_at.tzinfo is None:
            raise InvalidTaskInput("Prepared accounting time must include a timezone")
        finalized_at = finalized_at.astimezone(timezone.utc)
        if before_task.id != after_task.id:
            raise InvalidTaskInput("Prepared accounting Task identity changed")
        if before_attempt.id != after_attempt.id or before_attempt.run_id != after_attempt.run_id:
            raise InvalidTaskInput("Prepared accounting Attempt identity changed")
        if before_attempt.run_id != run_id:
            raise InvalidTaskInput("Prepared accounting Run identity is invalid")
        if (
            before_attempt.reserved_tokens != after_attempt.reserved_tokens
            or before_attempt.reserved_cost_micros != after_attempt.reserved_cost_micros
        ):
            raise InvalidTaskInput("Prepared accounting reservation changed")
        return cls(
            task_id=before_task.id,
            run_id=run_id,
            attempt_id=before_attempt.id,
            attempt_reserved_tokens=before_attempt.reserved_tokens,
            attempt_reserved_cost_micros=before_attempt.reserved_cost_micros,
            attempt_worker_id=before_attempt.worker_id,
            attempt_fencing_token=before_attempt.fencing_token,
            attempt_lease_token=before_attempt.lease_token,
            before_task_settled_tokens=before_task.settled_tokens,
            before_task_reserved_tokens=before_task.reserved_tokens,
            before_task_settled_cost_micros=before_task.settled_cost_micros,
            before_task_reserved_cost_micros=before_task.reserved_cost_micros,
            before_task_budget_revision=before_task.budget_revision,
            before_task_version=before_task.version,
            before_task_updated_at=before_task.updated_at,
            after_task_settled_tokens=after_task.settled_tokens,
            after_task_reserved_tokens=after_task.reserved_tokens,
            after_task_settled_cost_micros=after_task.settled_cost_micros,
            after_task_reserved_cost_micros=after_task.reserved_cost_micros,
            after_task_budget_revision=after_task.budget_revision,
            after_task_version=after_task.version,
            after_task_updated_at=after_task.updated_at,
            before_attempt_settled_tokens=before_attempt.settled_tokens,
            before_attempt_settled_cost_micros=before_attempt.settled_cost_micros,
            before_attempt_source=before_attempt.budget_settlement_source,
            after_attempt_settled_tokens=after_attempt.settled_tokens,
            after_attempt_settled_cost_micros=after_attempt.settled_cost_micros,
            after_attempt_source=after_attempt.budget_settlement_source,
            finalized_at=finalized_at,
        )

    def __post_init__(self) -> None:
        if not all(type(value) is UUID for value in (self.task_id, self.run_id, self.attempt_id)):
            raise InvalidTaskInput("Prepared accounting identity is invalid")
        for value in (
            self.attempt_reserved_tokens,
            self.attempt_reserved_cost_micros,
            self.before_task_settled_tokens,
            self.before_task_reserved_tokens,
            self.before_task_settled_cost_micros,
            self.before_task_reserved_cost_micros,
            self.before_task_budget_revision,
            self.before_task_version,
            self.after_task_settled_tokens,
            self.after_task_reserved_tokens,
            self.after_task_settled_cost_micros,
            self.after_task_reserved_cost_micros,
            self.after_task_budget_revision,
            self.after_task_version,
        ):
            if type(value) is not int or value < 0:
                raise InvalidTaskInput("Prepared accounting counters are invalid")
        if (
            not isinstance(self.attempt_worker_id, str)
            or not self.attempt_worker_id.strip()
            or type(self.attempt_fencing_token) is not int
            or self.attempt_fencing_token < 1
            or type(self.attempt_lease_token) is not UUID
        ):
            raise InvalidTaskInput("Prepared Attempt owner proof is invalid")
        for value in (self.before_task_updated_at, self.after_task_updated_at, self.finalized_at):
            if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
                raise InvalidTaskInput("Prepared accounting time must include a timezone")
        for value in (
            self.before_attempt_settled_tokens,
            self.before_attempt_settled_cost_micros,
            self.after_attempt_settled_tokens,
            self.after_attempt_settled_cost_micros,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise InvalidTaskInput("Prepared attempt accounting totals are invalid")
        for value in (self.before_attempt_source, self.after_attempt_source):
            if value is not None and not isinstance(value, BudgetSettlementSource):
                raise InvalidTaskInput("Prepared accounting source is invalid")


@dataclass(frozen=True)
class PreparedAccountingBatch:
    """Ordered, complete accounting proof for one business outcome."""

    transitions: tuple[PreparedAccountingTransition, ...]

    @classmethod
    def single(cls, transition: PreparedAccountingTransition) -> PreparedAccountingBatch:
        return cls((transition,))

    def __post_init__(self) -> None:
        if not isinstance(self.transitions, tuple) or not self.transitions:
            raise InvalidTaskInput("Prepared accounting batch must not be empty")
        if any(not isinstance(item, PreparedAccountingTransition) for item in self.transitions):
            raise InvalidTaskInput("Prepared accounting batch contains an invalid transition")
        first = self.transitions[0]
        seen_attempts: set[UUID] = set()
        seen_runs: set[UUID] = set()
        for index, transition in enumerate(self.transitions):
            if transition.task_id != first.task_id or transition.finalized_at != first.finalized_at:
                raise InvalidTaskInput("Prepared accounting batch scope is inconsistent")
            if transition.attempt_id in seen_attempts or transition.run_id in seen_runs:
                raise InvalidTaskInput("Prepared accounting batch contains duplicate identity")
            seen_attempts.add(transition.attempt_id)
            seen_runs.add(transition.run_id)
            if transition.after_task_version != transition.before_task_version + 1:
                raise InvalidTaskInput("Prepared accounting batch version delta is invalid")
            if index and self._task_state(
                self.transitions[index - 1], after=True
            ) != self._task_state(transition, after=False):
                raise InvalidTaskInput("Prepared accounting batch Task chain is broken")

    @staticmethod
    def _task_state(
        transition: PreparedAccountingTransition, *, after: bool
    ) -> tuple[int, int, int, int, int, int, datetime]:
        prefix = "after_" if after else "before_"
        return (
            getattr(transition, f"{prefix}task_settled_tokens"),
            getattr(transition, f"{prefix}task_reserved_tokens"),
            getattr(transition, f"{prefix}task_settled_cost_micros"),
            getattr(transition, f"{prefix}task_reserved_cost_micros"),
            getattr(transition, f"{prefix}task_budget_revision"),
            getattr(transition, f"{prefix}task_version"),
            getattr(transition, f"{prefix}task_updated_at"),
        )


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
        if not isinstance(self.progression_context, ProgressionContext):
            raise InvalidTaskInput("Business outcome progression context is invalid")
        if not isinstance(self.accounting_disposition, AccountingDisposition):
            raise InvalidTaskInput("Business outcome accounting disposition is invalid")
        if not isinstance(self.task_status, TaskStatus):
            raise InvalidTaskInput("Business outcome Task status is invalid")
        if not isinstance(self.run_status, RunStatus):
            raise InvalidTaskInput("Business outcome Run status is invalid")
        if not isinstance(self.attempt_status, AttemptStatus):
            raise InvalidTaskInput("Business outcome Attempt status is invalid")
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
            not self.reconciliation_reason.strip()
            or self.reconciliation_reason != self.reconciliation_reason.strip()
            or len(self.reconciliation_reason) > 512
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
        accounting_transition: PreparedAccountingTransition | None = None,
        accounting_batch: PreparedAccountingBatch | None = None,
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
            context,
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
        if accounting_transition is not None and accounting_batch is not None:
            raise InvalidTaskInput("Provide one accounting proof shape")
        if accounting_transition is not None:
            try:
                accounting_batch = PreparedAccountingBatch.single(accounting_transition)
            except InvalidTaskInput as exc:
                # Keep the compatibility argument's established transition
                # failure contract while batch callers fail at construction.
                raise InvalidTaskTransition(str(exc)) from exc
        if cancel_intent_present and (
            locked_run.runtime_authority != "managed"
            or terminal_phase is not KnownTerminalPhase.CANCELED
            or context
            not in {
                ProgressionContext.ORDINARY,
                ProgressionContext.DIRECT_RECONCILIATION,
            }
        ):
            raise InvalidTaskInput("Cancellation intent only applies to managed cancellation")
        # The domain policy clock is checked before any accounting or business mutation.
        locked_task.validate_policy_at(at)
        # A caller-owned in-memory UoW may still expose the before accounting
        # snapshot here; the batch validator below proves/adopts the after
        # snapshot atomically.  The legacy no-proof path keeps its direct
        # accounting validation.
        if accounting_batch is None:
            self._validate_accounting(
                locked_task, locked_attempt, disposition, context, terminal_phase
            )
        pre_task_status = locked_task.status

        managed = locked_run.runtime_authority == "managed"
        if context is ProgressionContext.DIRECT_RECONCILIATION:
            self._validate_reconciliation(locked_task, locked_run, locked_attempt, managed)
            # Reconciliation never accepts an ordinary accounting proof.  Run
            # this closed validation before its terminal mutations so a
            # caller cannot smuggle a batch into the reconciliation path.
            self._apply_prepared_accounting_batch(
                uow,
                locked_task,
                locked_run,
                locked_attempt,
                disposition=disposition,
                phase=terminal_phase,
                finalized_at=at,
                batch=accounting_batch,
            )
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
                        revision_number=locked_run.revision_number,
                        kind=ContinuationKind.REVIEWER,
                        at=at,
                    )
                )

        if pause_alignment:
            accounting_attempts = self._apply_prepared_accounting_batch(
                uow,
                locked_task,
                locked_run,
                locked_attempt,
                disposition=disposition,
                phase=effective_phase,
                finalized_at=at,
                batch=accounting_batch,
            )
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
                budget_rejection,
                disposition,
                causation_id,
                at,
                pre_task_status,
                accounting_batch=accounting_batch,
            )
        else:
            accounting_attempts = self._apply_prepared_accounting_batch(
                uow,
                locked_task,
                locked_run,
                locked_attempt,
                disposition=disposition,
                phase=effective_phase,
                finalized_at=at,
                batch=accounting_batch,
            )
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
        for saved_attempt in (
            accounting_attempts.values() if accounting_attempts else (locked_attempt,)
        ):
            uow.attempts.save(saved_attempt)
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

    @staticmethod
    def _apply_prepared_accounting_batch(
        uow: Any,
        task: Task,
        target_run: TaskRun,
        target_attempt: TaskAttempt,
        *,
        disposition: AccountingDisposition,
        phase: KnownTerminalPhase,
        finalized_at: datetime,
        batch: PreparedAccountingBatch | None,
    ) -> dict[UUID, TaskAttempt]:
        if batch is None:
            return {}
        if not isinstance(batch, PreparedAccountingBatch):
            raise InvalidTaskTransition("Accounting batch shape is invalid")
        transitions = batch.transitions
        if any(item.task_id != task.id for item in transitions):
            raise InvalidTaskTransition("Accounting batch Task identity is invalid")
        if transitions[0].attempt_id != target_attempt.id:
            raise InvalidTaskTransition("Accounting batch must start with target Attempt")
        target = next((item for item in transitions if item.attempt_id == target_attempt.id), None)
        if target is None:
            raise InvalidTaskTransition("Accounting batch does not contain target Attempt")
        if target.run_id != target_run.id:
            raise InvalidTaskTransition("Accounting batch target Run does not match outcome")
        if task.execution_mode in {TaskExecutionMode.DIRECT, TaskExecutionMode.REVIEWED}:
            if len(transitions) != 1:
                raise InvalidTaskTransition("DIRECT/REVIEWED outcomes allow one accounting proof")
        locked_attempts: dict[UUID, TaskAttempt] = {target_attempt.id: target_attempt}
        for transition in transitions:
            if transition.attempt_id == target_attempt.id:
                continue
            if task.execution_mode is not TaskExecutionMode.COORDINATED:
                raise InvalidTaskTransition("Only coordinated outcomes allow sibling proofs")
            if transition.after_attempt_source is not BudgetSettlementSource.RELEASED:
                raise InvalidTaskTransition("Coordinated sibling proof must be RELEASED")
            sibling_run = uow.runs.get(transition.run_id, for_update=True)
            sibling_attempt = uow.attempts.get(transition.attempt_id, for_update=True)
            latest = uow.attempts.latest_for_run(transition.run_id, for_update=True)
            if (
                sibling_run is None
                or sibling_run.task_id != task.id
                or sibling_run.role is not RunRole.EXECUTOR
                or sibling_run.subtask_id is None
                or sibling_attempt is None
                or sibling_attempt.run_id != sibling_run.id
                or latest is None
                or latest.id != sibling_attempt.id
                or sibling_run.status
                not in {
                    RunStatus.RUNNING,
                    RunStatus.PAUSE_REQUESTED,
                    RunStatus.PAUSED,
                    RunStatus.WAITING_REMOTE,
                }
                or sibling_attempt.status not in {AttemptStatus.RUNNING, AttemptStatus.PAUSED}
                or (
                    sibling_run.status is RunStatus.PAUSED
                    and sibling_attempt.status is not AttemptStatus.PAUSED
                )
            ):
                raise InvalidTaskTransition("Coordinated sibling accounting state is invalid")
            locked_attempts[sibling_attempt.id] = sibling_attempt

        def task_state(item: PreparedAccountingTransition, *, after: bool) -> tuple[Any, ...]:
            return PreparedAccountingBatch._task_state(item, after=after)

        def attempt_state(item: TaskAttempt) -> tuple[Any, ...]:
            return (item.settled_tokens, item.settled_cost_micros, item.budget_settlement_source)

        def expected_attempt_state(
            item: PreparedAccountingTransition, *, after: bool
        ) -> tuple[Any, ...]:
            prefix = "after_" if after else "before_"
            return (
                getattr(item, f"{prefix}attempt_settled_tokens"),
                getattr(item, f"{prefix}attempt_settled_cost_micros"),
                getattr(item, f"{prefix}attempt_source"),
            )

        for transition in transitions:
            proof_disposition = (
                disposition if transition is target else AccountingDisposition.RELEASED
            )
            proof_phase = phase if transition is target else KnownTerminalPhase.FAILED
            if proof_disposition is AccountingDisposition.NOT_APPLICABLE:
                raise InvalidTaskTransition("No-budget outcomes cannot carry accounting batch")
            if (
                proof_phase is KnownTerminalPhase.SUCCEEDED
                and proof_disposition is not AccountingDisposition.SETTLED
            ):
                raise InvalidTaskTransition("Successful accounting proof must be settled")
            if (
                proof_phase is not KnownTerminalPhase.SUCCEEDED
                and proof_disposition is not AccountingDisposition.RELEASED
            ):
                raise InvalidTaskTransition("Failed accounting proof must be released")
            if task.budget is None:
                raise InvalidTaskTransition("Accounting batch requires a Task budget")
            if transition.finalized_at != finalized_at:
                raise InvalidTaskTransition("Accounting batch clock is inconsistent")
            if transition.after_task_updated_at != finalized_at:
                raise InvalidTaskTransition("Accounting batch clock does not match outcome")
            if finalized_at < transition.before_task_updated_at:
                raise InvalidTaskTransition("Accounting batch clock moves backwards")
            if transition.after_task_version != transition.before_task_version + 1:
                raise InvalidTaskTransition("Accounting proof version delta is invalid")
            if transition.after_task_budget_revision != transition.before_task_budget_revision:
                raise InvalidTaskTransition("Accounting proof revision changed unexpectedly")
            if transition.before_task_reserved_tokens < transition.attempt_reserved_tokens:
                raise InvalidTaskTransition("Accounting token reservation is invalid")
            if (
                transition.before_task_reserved_cost_micros
                < transition.attempt_reserved_cost_micros
            ):
                raise InvalidTaskTransition("Accounting cost reservation is invalid")
            if (
                transition.after_task_reserved_tokens
                != transition.before_task_reserved_tokens - transition.attempt_reserved_tokens
            ):
                raise InvalidTaskTransition("Accounting token delta is invalid")
            if (
                transition.after_task_reserved_cost_micros
                != transition.before_task_reserved_cost_micros
                - transition.attempt_reserved_cost_micros
            ):
                raise InvalidTaskTransition("Accounting cost delta is invalid")
            if (
                transition.before_attempt_source is not None
                or transition.before_attempt_settled_tokens is not None
                or transition.before_attempt_settled_cost_micros is not None
            ):
                raise InvalidTaskTransition("Accounting proof must start unsettled")
            if proof_disposition is AccountingDisposition.RELEASED:
                if (
                    transition.after_attempt_source is not BudgetSettlementSource.RELEASED
                    or transition.after_attempt_settled_tokens != 0
                    or transition.after_attempt_settled_cost_micros != 0
                    or transition.after_task_settled_tokens != transition.before_task_settled_tokens
                    or transition.after_task_settled_cost_micros
                    != transition.before_task_settled_cost_micros
                ):
                    raise InvalidTaskTransition("Accounting release delta is invalid")
            else:
                if (
                    transition.after_attempt_source
                    not in {
                        BudgetSettlementSource.ACTUAL,
                        BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
                    }
                    or transition.after_attempt_settled_tokens is None
                    or transition.after_attempt_settled_cost_micros is None
                ):
                    raise InvalidTaskTransition("Accounting settlement totals are invalid")
                if (
                    transition.after_task_settled_tokens
                    != transition.before_task_settled_tokens
                    + transition.after_attempt_settled_tokens
                    or transition.after_task_settled_cost_micros
                    != transition.before_task_settled_cost_micros
                    + transition.after_attempt_settled_cost_micros
                ):
                    raise InvalidTaskTransition("Accounting settlement delta is invalid")
            locked_attempt = locked_attempts[transition.attempt_id]
            if (
                locked_attempt.reserved_tokens != transition.attempt_reserved_tokens
                or locked_attempt.reserved_cost_micros != transition.attempt_reserved_cost_micros
                or locked_attempt.worker_id != transition.attempt_worker_id
                or locked_attempt.fencing_token != transition.attempt_fencing_token
                or locked_attempt.lease_token != transition.attempt_lease_token
            ):
                raise InvalidTaskTransition("Accounting Attempt owner proof does not match")

        first = transitions[0]
        final = transitions[-1]
        current_task = (
            task.settled_tokens,
            task.reserved_tokens,
            task.settled_cost_micros,
            task.reserved_cost_micros,
            task.budget_revision,
            task.version,
            task.updated_at,
        )
        before_task = task_state(first, after=False)
        after_task = task_state(final, after=True)
        all_before = current_task == before_task and all(
            attempt_state(locked_attempts[item.attempt_id])
            == expected_attempt_state(item, after=False)
            for item in transitions
        )
        all_after = current_task == after_task and all(
            attempt_state(locked_attempts[item.attempt_id])
            == expected_attempt_state(item, after=True)
            for item in transitions
        )
        if not all_before and not all_after:
            raise InvalidTaskTransition("Accounting batch contains a partial or stale state")
        if all_after:
            return locked_attempts
        for field, value in (
            ("settled_tokens", final.after_task_settled_tokens),
            ("reserved_tokens", final.after_task_reserved_tokens),
            ("settled_cost_micros", final.after_task_settled_cost_micros),
            ("reserved_cost_micros", final.after_task_reserved_cost_micros),
            ("budget_revision", final.after_task_budget_revision),
            ("version", final.after_task_version),
            ("updated_at", final.after_task_updated_at),
        ):
            setattr(task, field, value)
        for item in transitions:
            locked_attempt = locked_attempts[item.attempt_id]
            locked_attempt.settled_tokens = item.after_attempt_settled_tokens
            locked_attempt.settled_cost_micros = item.after_attempt_settled_cost_micros
            locked_attempt.budget_settlement_source = item.after_attempt_source
        return locked_attempts

    def _apply_legacy_coordinated(
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
        causation_id: UUID,
        at: datetime,
        pre_task_status: TaskStatus,
        accounting_batch: PreparedAccountingBatch | None = None,
    ) -> BusinessOutcomeApplication:
        subtasks, runs, latest_attempts = self._lock_coordinated_members(uow, task)
        by_subtask = {subtask.id: subtask for subtask in subtasks}
        by_run = {candidate.id: candidate for candidate in runs}
        if by_run.get(run.id) != run or latest_attempts.get(run.id) != attempt:
            raise InvalidTaskTransition("Coordinated target changed while locking")
        target_subtask = None
        if run.role is RunRole.EXECUTOR:
            if run.subtask_id is None:
                raise InvalidTaskTransition("Coordinated executor Run has no Subtask binding")
            target_subtask = by_subtask.get(run.subtask_id)
            if target_subtask is None or target_subtask.status is not SubtaskStatus.RUNNING:
                raise InvalidTaskTransition("Coordinated Subtask is not running")
            if target_subtask.current_run_id != run.id:
                raise InvalidTaskTransition("Coordinated Subtask binding is stale")
        elif run.role is not RunRole.SUPERVISOR or run.subtask_id is not None:
            raise InvalidTaskTransition("Coordinated Run role/binding is invalid")

        if run.role is RunRole.SUPERVISOR:
            if accounting_batch is not None and len(accounting_batch.transitions) != 1:
                raise InvalidTaskTransition("Supervisor outcomes allow one accounting proof")
            terminal_runs = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
            if any(
                candidate.id != run.id and candidate.status not in terminal_runs
                for candidate in runs
            ):
                raise InvalidTaskTransition("Supervisor outcome has nonterminal sibling Runs")
            if any(
                subtask.status
                not in {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.CANCELED}
                for subtask in subtasks
            ):
                raise InvalidTaskTransition("Supervisor outcome requires terminal Subtasks")
            accounting_attempts = self._apply_prepared_accounting_batch(
                uow,
                task,
                run,
                attempt,
                disposition=disposition,
                phase=phase,
                finalized_at=at,
                batch=accounting_batch,
            )
            if accounting_attempts:
                attempt = accounting_attempts.get(attempt.id, attempt)
                for candidate in runs:
                    latest = latest_attempts.get(candidate.id)
                    if latest is not None and latest.id in accounting_attempts:
                        latest_attempts[candidate.id] = accounting_attempts[latest.id]
            self._apply_run_and_attempt(task, run, attempt, phase, output, safe_error, at)
            if phase is KnownTerminalPhase.SUCCEEDED:
                assert output is not None
                if budget_rejection is None:
                    task.complete(run.id, output, at=at)
                else:
                    task.wait_for_budget(budget_rejection, candidate_output=output, at=at)
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
            uow.tasks.save(task)
            uow.runs.save(run)
            for saved_attempt in (
                accounting_attempts.values() if accounting_attempts else (attempt,)
            ):
                uow.attempts.save(saved_attempt)
            new_runs: list[TaskRun] = []
        else:
            assert target_subtask is not None
            is_success = phase is KnownTerminalPhase.SUCCEEDED
            if is_success and budget_rejection is None:
                if self._coordinated_scheduler is None:
                    raise InvalidTaskTransition("Coordinated success requires a scheduler plan")
                schedule_plan = self._coordinated_scheduler.plan(
                    uow,
                    task,
                    completing_subtask_id=target_subtask.id,
                    completion_output=output,
                    at=at,
                    causation_id=causation_id,
                )
                sibling_state = None
            else:
                schedule_plan = None
                sibling_state = self._validate_coordinated_shutdown(
                    task, run, subtasks, by_run, latest_attempts, accounting_batch
                )

            accounting_attempts = self._apply_prepared_accounting_batch(
                uow,
                task,
                run,
                attempt,
                disposition=disposition,
                phase=phase,
                finalized_at=at,
                batch=accounting_batch,
            )
            if accounting_attempts:
                attempt = accounting_attempts.get(attempt.id, attempt)
                for candidate in runs:
                    latest = latest_attempts.get(candidate.id)
                    if latest is not None and latest.id in accounting_attempts:
                        latest_attempts[candidate.id] = accounting_attempts[latest.id]
            self._apply_run_and_attempt(task, run, attempt, phase, output, safe_error, at)
            if is_success:
                assert output is not None
                target_subtask.complete(run.id, output, at=at)
                if budget_rejection is not None:
                    task.wait_for_budget(budget_rejection, candidate_output=None, at=at)
            elif phase is KnownTerminalPhase.CANCELED:
                target_subtask.cancel(at=at)
                task.cancel(at=at)
            else:
                error = safe_error or (
                    "runtime.timed_out"
                    if phase is KnownTerminalPhase.TIMED_OUT
                    else "runtime.failed"
                )
                target_subtask.fail(run.id, error, at=at)
                task.fail_coordination(error, at=at)

            if sibling_state is not None:
                canceled_subtasks, canceled_runs, canceled_attempts = (
                    self._cancel_coordinated_siblings(*sibling_state, at=at)
                )
                for canceled in canceled_subtasks:
                    uow.subtasks.save(canceled)
                for canceled in canceled_runs:
                    uow.runs.save(canceled)
                accounting_ids = set(accounting_attempts)
                for canceled in canceled_attempts:
                    if canceled.id not in accounting_ids:
                        uow.attempts.save(canceled)
            uow.subtasks.save(target_subtask)
            uow.tasks.save(task)
            uow.runs.save(run)
            for saved_attempt in (
                accounting_attempts.values() if accounting_attempts else (attempt,)
            ):
                uow.attempts.save(saved_attempt)
            if schedule_plan is not None:
                # The target transition is saved before the scheduler CAS
                # phase, which owns continuations and RunRequested messages.
                # Accounting adoption may advance Task.version inside the
                # same UoW.  Refresh only that expected CAS token; every other
                # plan snapshot remains unchanged and is still revalidated.
                persisted_task = uow.tasks.get(task.id)
                if persisted_task is None:
                    raise InvalidTaskTransition("Coordinated Task disappeared before scheduling")
                if persisted_task.version != schedule_plan.task_version:
                    if accounting_batch is None:
                        raise InvalidTaskTransition("Coordinated schedule plan is stale")
                    first_proof = accounting_batch.transitions[0]
                    final_proof = accounting_batch.transitions[-1]
                    if (
                        schedule_plan.task_version != first_proof.before_task_version
                        or task.version != final_proof.after_task_version
                        or persisted_task.version != final_proof.after_task_version
                    ):
                        raise InvalidTaskTransition("Coordinated schedule plan is stale")
                    schedule_plan = replace(
                        schedule_plan,
                        task_version=persisted_task.version,
                    )
                new_runs = list(self._coordinated_scheduler.apply(uow, schedule_plan))
                current_task = uow.tasks.get(task.id)
                if current_task is not None:
                    task.__dict__.update(current_task.__dict__)
            else:
                new_runs = []

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
    def _lock_coordinated_members(
        uow: Any, task: Task
    ) -> tuple[list[Any], list[TaskRun], dict[UUID, TaskAttempt | None]]:
        """Lock the complete coordination projection before a stop mutation."""
        listed_subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        subtask_ids = [subtask.id for subtask in listed_subtasks]
        if len(set(subtask_ids)) != len(subtask_ids):
            raise InvalidTaskTransition("Coordinated Subtask set contains duplicates")
        subtasks = []
        for subtask_id in subtask_ids:
            locked = uow.subtasks.get(subtask_id, for_update=True)
            if locked is None:
                raise InvalidTaskTransition("Coordinated Subtask set changed while locking")
            subtasks.append(locked)
        if {subtask.id for subtask in subtasks} != set(subtask_ids):
            raise InvalidTaskTransition("Coordinated Subtask set changed while locking")

        listed_runs = uow.runs.list_for_task(task.id)
        run_ids = [candidate.id for candidate in listed_runs]
        if len(set(run_ids)) != len(run_ids):
            raise InvalidTaskTransition("Coordinated Run set contains duplicates")
        runs = []
        for run_id in run_ids:
            locked = uow.runs.get(run_id, for_update=True)
            if locked is None:
                raise InvalidTaskTransition("Coordinated Run set changed while locking")
            if locked.task_id != task.id:
                raise InvalidTaskTransition("Coordinated Run is not owned by Task")
            runs.append(locked)
        if {candidate.id for candidate in runs} != set(run_ids):
            raise InvalidTaskTransition("Coordinated Run set changed while locking")
        latest_attempts = {
            candidate.id: uow.attempts.latest_for_run(candidate.id, for_update=True)
            for candidate in runs
        }
        for candidate in runs:
            latest = latest_attempts[candidate.id]
            if latest is not None and latest.run_id != candidate.id:
                raise InvalidTaskTransition("Coordinated Attempt binding is inconsistent")
        by_subtask = {subtask.id: subtask for subtask in subtasks}
        by_run = {candidate.id: candidate for candidate in runs}
        for subtask in subtasks:
            if subtask.task_id != task.id:
                raise InvalidTaskTransition("Coordinated Subtask is not owned by Task")
            if subtask.current_run_id is not None:
                bound_run = by_run.get(subtask.current_run_id)
                if bound_run is None or bound_run.subtask_id != subtask.id:
                    raise InvalidTaskTransition("Coordinated Subtask Run binding is inconsistent")
        for candidate in runs:
            if candidate.role is RunRole.EXECUTOR:
                if candidate.subtask_id is None or candidate.subtask_id not in by_subtask:
                    raise InvalidTaskTransition("Coordinated executor Run binding is invalid")
            elif candidate.role is RunRole.SUPERVISOR:
                if candidate.subtask_id is not None:
                    raise InvalidTaskTransition("Coordinated Supervisor Run binding is invalid")
            else:
                raise InvalidTaskTransition("Coordinated Run role is invalid")
        return subtasks, runs, latest_attempts

    @staticmethod
    def _validate_coordinated_shutdown(
        task: Task,
        target_run: TaskRun,
        subtasks: list[Any],
        runs: dict[UUID, TaskRun],
        latest_attempts: dict[UUID, TaskAttempt | None],
        accounting_batch: PreparedAccountingBatch | None = None,
    ) -> tuple[list[Any], list[TaskRun], dict[UUID, TaskAttempt | None]]:
        """Validate sibling accounting and return the fixed set to cancel."""
        nonterminal_runs = {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSE_REQUESTED,
            RunStatus.PAUSED,
            RunStatus.WAITING_REMOTE,
        }
        active_attempts = {AttemptStatus.RUNNING, AttemptStatus.PAUSED}
        proof_by_attempt = (
            {item.attempt_id: item for item in accounting_batch.transitions}
            if accounting_batch is not None
            else {}
        )
        siblings = [candidate for candidate in runs.values() if candidate.id != target_run.id]
        for candidate in siblings:
            if candidate.status not in nonterminal_runs:
                continue
            if candidate.role is not RunRole.EXECUTOR or candidate.subtask_id is None:
                raise InvalidTaskTransition("Coordinated sibling Run binding is invalid")
            sibling_attempt = latest_attempts.get(candidate.id)
            if candidate.status is not RunStatus.QUEUED and sibling_attempt is None:
                raise InvalidTaskTransition("Active coordinated sibling has no Attempt")
            if candidate.status is not RunStatus.QUEUED and (
                sibling_attempt is None or sibling_attempt.status not in active_attempts
            ):
                raise InvalidTaskTransition("Active coordinated sibling Attempt is not active")
            if sibling_attempt is not None and sibling_attempt.status in active_attempts:
                expected_source = (
                    BudgetSettlementSource.RELEASED if task.budget is not None else None
                )
                proof = proof_by_attempt.get(sibling_attempt.id)
                if proof is not None:
                    if sibling_attempt.budget_settlement_source not in {
                        proof.before_attempt_source,
                        proof.after_attempt_source,
                    }:
                        raise InvalidTaskTransition(
                            "Coordinated sibling accounting proof state is invalid"
                        )
                elif sibling_attempt.budget_settlement_source is not expected_source:
                    raise InvalidTaskTransition("Coordinated sibling accounting is not released")
        return subtasks, siblings, latest_attempts

    @staticmethod
    def _cancel_coordinated_siblings(
        subtasks: list[Any],
        siblings: list[TaskRun],
        latest_attempts: dict[UUID, TaskAttempt | None],
        *,
        at: datetime,
    ) -> tuple[list[Any], list[TaskRun], list[TaskAttempt]]:
        terminal_subtasks = {
            SubtaskStatus.COMPLETED,
            SubtaskStatus.FAILED,
            SubtaskStatus.CANCELED,
        }
        terminal_runs = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
        canceled_subtasks = []
        for subtask in subtasks:
            if subtask.status not in terminal_subtasks:
                subtask.cancel(at=at)
                canceled_subtasks.append(subtask)
        canceled_runs = []
        canceled_attempts = []
        for candidate in siblings:
            if candidate.status not in terminal_runs:
                candidate.cancel(at=at)
                canceled_runs.append(candidate)
            sibling_attempt = latest_attempts.get(candidate.id)
            if sibling_attempt is not None and sibling_attempt.status is AttemptStatus.RUNNING:
                sibling_attempt.cancel(at=at)
                canceled_attempts.append(sibling_attempt)
        return canceled_subtasks, canceled_runs, canceled_attempts

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
        context: ProgressionContext,
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
        allowed_budget_rejections = (
            {"budget_deadline_exceeded"}
            if context is ProgressionContext.DIRECT_RECONCILIATION
            else {
                "budget_deadline_exceeded",
                "budget_token_limit_exhausted",
                "budget_cost_limit_exhausted",
                "budget_run_limit_exhausted",
            }
        )
        if (
            budget_rejection is not None
            and budget_rejection.strip() not in allowed_budget_rejections
        ):
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
        if (
            supplied_run.runtime_authority != run.runtime_authority
            or supplied_run.runtime_version_id != run.runtime_version_id
            or supplied_run.runtime_execution_id != run.runtime_execution_id
            or supplied_run.runtime_execution_intent_id != run.runtime_execution_intent_id
        ):
            raise InvalidTaskTransition("Outcome Run authority or Runtime binding changed")
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
    "PreparedAccountingBatch",
    "PreparedAccountingTransition",
    "ProgressionContext",
]
