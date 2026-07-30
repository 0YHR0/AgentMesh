from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskAggregate, TaskApplicationService
from agentmesh.domain.company import CompanyStatus, ResourceStatus
from agentmesh.domain.company_goals import InitiativeStatus, InitiativeTaskLink
from agentmesh.domain.company_operations import (
    CompanyOperation,
    MissedSchedulePolicy,
    OccurrenceStatus,
    OperationException,
    OperationOccurrence,
    OperationStatus,
    OperationTriggerState,
)
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import (
    CompanyOperationConflict,
    CompanyOperationNotFound,
    InvalidCompanyOperation,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.registry import AgentDefinitionLifecycle, AgentVersionStatus
from agentmesh.domain.tasks import TaskExecutionMode, TaskStatus, utc_now
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class OperationLaunch:
    occurrence: OperationOccurrence
    task: TaskAggregate | None


@dataclass(frozen=True)
class OperationSnapshot:
    operation: CompanyOperation
    trigger_state: OperationTriggerState | None
    occurrences: list[OperationOccurrence]
    exceptions: list[OperationException]


class CompanyOperationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        task_service: TaskApplicationService,
        tenant_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._task_service = task_service
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates

    def create_operation(
        self, company_id: UUID, *, activated_at: datetime | None = None, **values: Any
    ) -> CompanyOperation:
        self._require_enabled()
        operation = CompanyOperation.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            unit = uow.company_model.get_unit(operation.organization_unit_id)
            if (
                unit is None
                or unit.company_id != company_id
                or unit.status is not ResourceStatus.ACTIVE
            ):
                raise InvalidCompanyOperation(
                    "Operation owner must be an active Organization Unit"
                )
            if uow.company_operations.get_operation_by_key(company_id, operation.key):
                raise CompanyOperationConflict("Company already contains this Operation key")
            if operation.initiative_id is not None:
                initiative = uow.company_goals.get_initiative(operation.initiative_id)
                if (
                    initiative is None
                    or initiative.company_id != company_id
                    or initiative.status is not InitiativeStatus.ACTIVE
                ):
                    raise InvalidCompanyOperation(
                        "Operation Initiative must be active and belong to the Company"
                    )
            for position_id in operation.position_bindings:
                position = uow.company_model.get_position(position_id)
                if (
                    position is None
                    or position.company_id != company_id
                    or position.status is not ResourceStatus.ACTIVE
                ):
                    raise InvalidCompanyOperation(
                        "Operation position bindings must be active Company Positions"
                    )
            uow.company_operations.add_operation(operation)
            self._emit(uow, "operation.created", operation, company_id)
            uow.commit()
        if activated_at is not None:
            return self.transition_operation(
                company_id, operation.id, "activate", now=activated_at
            )
        return operation

    def transition_operation(
        self,
        company_id: UUID,
        operation_id: UUID,
        action: str,
        *,
        now: datetime | None = None,
    ) -> CompanyOperation:
        self._require_enabled()
        transition_at = now or utc_now()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            operation = self._operation(uow, company_id, operation_id, for_update=True)
            state = uow.company_operations.get_trigger_state(
                operation.id, for_update=True
            )
            if action == "activate":
                operation.activate()
                if state is None:
                    state = OperationTriggerState.create(
                        operation, activated_at=transition_at
                    )
                    uow.company_operations.add_trigger_state(state)
                else:
                    state.next_due_at = operation.first_due_at(transition_at)
                    state.paused_reason = None
                    state.updated_at = transition_at
                    uow.company_operations.save_trigger_state(state)
            elif action == "pause":
                operation.pause()
                if state is not None:
                    state.paused_reason = "operator"
                    state.updated_at = transition_at
                    uow.company_operations.save_trigger_state(state)
            elif action == "disable":
                operation.disable()
                if state is not None:
                    state.next_due_at = None
                    state.paused_reason = "disabled"
                    state.updated_at = transition_at
                    uow.company_operations.save_trigger_state(state)
            else:
                raise InvalidCompanyOperation(f"Unknown Operation action '{action}'")
            uow.company_operations.save_operation(operation)
            self._emit(uow, f"operation.{action}d", operation, company_id)
            uow.commit()
            return operation

    def list_operations(self, company_id: UUID) -> list[CompanyOperation]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.company_operations.list_operations(company_id)

    def activate_staffed_operations(
        self,
        company_id: UUID,
        *,
        operation_keys: list[str],
        activated_at: datetime | None = None,
    ) -> list[CompanyOperation]:
        self._require_enabled()
        self._feature_gates.require(Feature.COORDINATED_EXECUTION)
        transition_at = activated_at or utc_now()
        if transition_at.tzinfo is None:
            raise InvalidCompanyOperation(
                "Operation activation timestamp must be timezone-aware"
            )
        normalized = [value.strip() for value in operation_keys if value.strip()]
        if len(set(normalized)) != len(normalized):
            raise InvalidCompanyOperation("Operation activation keys must be unique")
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            uow.idempotency.lock(
                f"staffed-operation-activation:{company_id}", self._tenant_id
            )
            selected = []
            if normalized:
                for key in normalized:
                    operation = uow.company_operations.get_operation_by_key(
                        company_id, key
                    )
                    if operation is None:
                        raise CompanyOperationNotFound(
                            f"Operation '{key}' was not found"
                        )
                    selected.append(
                        self._operation(
                            uow, company_id, operation.id, for_update=True
                        )
                    )
            else:
                selected = [
                    self._operation(
                        uow, company_id, operation.id, for_update=True
                    )
                    for operation in uow.company_operations.list_operations(
                        company_id
                    )
                    if operation.status
                    in {
                        OperationStatus.DRAFT,
                        OperationStatus.PAUSED,
                        OperationStatus.ACTIVE,
                    }
                ]
            if not selected:
                raise InvalidCompanyOperation(
                    "No Operations are available for activation"
                )
            blockers: list[str] = []
            for operation in selected:
                try:
                    self._resolve_appointed_workers(uow, operation)
                except InvalidCompanyOperation as exc:
                    blockers.append(f"{operation.key}: {exc}")
            if blockers:
                raise InvalidCompanyOperation(
                    "Operation staffing preflight failed: " + "; ".join(blockers)
                )
            activated: list[CompanyOperation] = []
            for operation in selected:
                if operation.status is OperationStatus.ACTIVE:
                    activated.append(operation)
                    continue
                operation.activate()
                state = uow.company_operations.get_trigger_state(
                    operation.id, for_update=True
                )
                if state is None:
                    state = OperationTriggerState.create(
                        operation, activated_at=transition_at
                    )
                    uow.company_operations.add_trigger_state(state)
                else:
                    state.next_due_at = operation.first_due_at(transition_at)
                    state.paused_reason = None
                    state.updated_at = transition_at
                    uow.company_operations.save_trigger_state(state)
                uow.company_operations.save_operation(operation)
                self._emit(
                    uow,
                    "operation.activated",
                    operation,
                    company_id,
                    {"activation_mode": "staffed-workforce"},
                )
                activated.append(operation)
            uow.commit()
            return activated

    def get_operation(
        self, company_id: UUID, operation_id: UUID
    ) -> OperationSnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            operation = self._operation(uow, company_id, operation_id)
            return OperationSnapshot(
                operation=operation,
                trigger_state=uow.company_operations.get_trigger_state(operation.id),
                occurrences=uow.company_operations.list_occurrences(operation.id),
                exceptions=uow.company_operations.list_exceptions(operation.id),
            )

    def trigger_manual(
        self,
        company_id: UUID,
        operation_id: UUID,
        *,
        event_id: str,
        scheduled_at: datetime | None = None,
    ) -> OperationLaunch:
        self._require_enabled()
        fired_at = scheduled_at or utc_now()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            operation = self._operation(uow, company_id, operation_id)
            if operation.status is not OperationStatus.ACTIVE:
                raise CompanyOperationConflict("Only an active Operation can be triggered")
        occurrence = self._ensure_occurrence(
            operation,
            scheduled_at=fired_at,
            identity=f"manual:{event_id.strip()}",
        )
        return self._launch_occurrence(operation, occurrence)

    def dispatch_due(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> list[OperationLaunch]:
        self._require_enabled()
        evaluated_at = now or utc_now()
        if evaluated_at.tzinfo is None:
            raise InvalidCompanyOperation("Dispatch time must be timezone-aware")
        if not 1 <= limit <= 500:
            raise InvalidCompanyOperation("Dispatch limit must be between 1 and 500")
        pending: list[tuple[CompanyOperation, OperationOccurrence]] = []
        with self._uow_factory() as uow:
            for operation, occurrence, exception in uow.company_operations.list_retryable(
                evaluated_at, tenant_id=self._tenant_id, limit=limit
            ):
                exception.resolved_at = evaluated_at
                uow.company_operations.save_exception(exception)
                pending.append((operation, occurrence))
            for operation, state in uow.company_operations.list_due(
                evaluated_at,
                tenant_id=self._tenant_id,
                limit=max(0, limit - len(pending)),
            ):
                scheduled = self._missed_occurrences(operation, state, evaluated_at)
                for at, status, detail in scheduled:
                    identity = at.isoformat()
                    key = self._occurrence_key(operation, identity)
                    occurrence = uow.company_operations.get_occurrence_by_key(
                        operation.id, key
                    )
                    if occurrence is None:
                        occurrence = OperationOccurrence.create(
                            operation, occurrence_key=key, scheduled_at=at
                        )
                        occurrence.status = status
                        occurrence.detail = detail
                        uow.company_operations.add_occurrence(occurrence)
                    if occurrence.status is OccurrenceStatus.PENDING:
                        pending.append((operation, occurrence))
                interval = int(operation.trigger_definition["interval_seconds"])
                next_due = state.next_due_at
                while next_due is not None and next_due <= evaluated_at:
                    next_due += timedelta(seconds=interval)
                state.next_due_at = next_due
                state.last_evaluated_at = evaluated_at
                state.last_fired_at = scheduled[-1][0] if scheduled else state.last_fired_at
                state.fencing_token += 1
                state.updated_at = evaluated_at
                if operation.missed_policy is MissedSchedulePolicy.REQUIRE_REVIEW:
                    state.paused_reason = "missed-schedule-review"
                    operation.pause()
                    uow.company_operations.save_operation(operation)
                uow.company_operations.save_trigger_state(state)
            uow.commit()
        return [
            self._launch_occurrence(operation, occurrence)
            for operation, occurrence in pending
        ]

    def _missed_occurrences(
        self,
        operation: CompanyOperation,
        state: OperationTriggerState,
        now: datetime,
    ) -> list[tuple[datetime, OccurrenceStatus, dict[str, Any]]]:
        if state.next_due_at is None:
            return []
        interval = timedelta(seconds=int(operation.trigger_definition["interval_seconds"]))
        due_count = int((now - state.next_due_at) // interval) + 1
        if due_count == 1:
            return [(state.next_due_at, OccurrenceStatus.PENDING, {})]
        latest = state.next_due_at + interval * (due_count - 1)
        if operation.missed_policy is MissedSchedulePolicy.SKIP:
            return [
                (
                    latest,
                    OccurrenceStatus.SKIPPED,
                    {"reason": "missed-schedule", "missed_count": due_count},
                )
            ]
        if operation.missed_policy is MissedSchedulePolicy.LATEST:
            return [
                (
                    state.next_due_at,
                    OccurrenceStatus.SKIPPED,
                    {"reason": "superseded", "missed_count": due_count - 1},
                ),
                (latest, OccurrenceStatus.PENDING, {}),
            ]
        if operation.missed_policy is MissedSchedulePolicy.REQUIRE_REVIEW:
            return [
                (
                    latest,
                    OccurrenceStatus.REVIEW_REQUIRED,
                    {"missed_count": due_count},
                )
            ]
        first_selected = max(0, due_count - operation.catch_up_limit)
        return [
            (
                state.next_due_at + interval * index,
                OccurrenceStatus.PENDING,
                {"missed_count": due_count} if index == first_selected else {},
            )
            for index in range(first_selected, due_count)
        ]

    def _ensure_occurrence(
        self,
        operation: CompanyOperation,
        *,
        scheduled_at: datetime,
        identity: str,
    ) -> OperationOccurrence:
        key = self._occurrence_key(operation, identity)
        with self._uow_factory() as uow:
            existing = uow.company_operations.get_occurrence_by_key(operation.id, key)
            if existing is not None:
                return existing
            occurrence = OperationOccurrence.create(
                operation, occurrence_key=key, scheduled_at=scheduled_at
            )
            uow.company_operations.add_occurrence(occurrence)
            uow.commit()
            return occurrence

    def _launch_occurrence(
        self, operation: CompanyOperation, occurrence: OperationOccurrence
    ) -> OperationLaunch:
        if occurrence.status is OccurrenceStatus.TASK_CREATED:
            return OperationLaunch(
                occurrence=occurrence,
                task=(
                    self._task_service.get_task(occurrence.task_id)
                    if occurrence.task_id
                    else None
                ),
            )
        if occurrence.status is not OccurrenceStatus.PENDING:
            return OperationLaunch(occurrence=occurrence, task=None)
        try:
            self._admit_occurrence(operation, occurrence)
            coordinated_plan = None
            execution_mode = TaskExecutionMode.DIRECT
            workforce_context: list[dict[str, str]] = []
            if operation.position_bindings:
                coordinated_plan, workforce_context = self._appointed_plan(
                    operation
                )
                execution_mode = TaskExecutionMode.COORDINATED
            task = self._task_service.create_task(
                operation.objective_template,
                input={
                    **operation.input_template,
                    "company_context": {
                        "company_id": str(operation.company_id),
                        "organization_unit_id": str(operation.organization_unit_id),
                        "initiative_id": (
                            str(operation.initiative_id)
                            if operation.initiative_id
                            else None
                        ),
                        "operation_id": str(operation.id),
                        "operation_version": operation.version,
                        "operation_digest": operation.content_digest,
                        "occurrence_key": occurrence.occurrence_key,
                        "scheduled_at": occurrence.scheduled_at.isoformat(),
                        "workforce": workforce_context,
                    },
                },
                execution_mode=execution_mode,
                coordinated_plan=coordinated_plan,
                goal_constraints=(
                    (
                        "Use only the appointed Position responsibilities and "
                        "verified Agent capabilities.",
                        "External writes and commercial commitments require "
                        "separate approval.",
                    )
                    if coordinated_plan
                    else ()
                ),
                goal_success_criteria=(
                    (
                        "Every appointed Position produces an attributable "
                        "Subtask result.",
                    )
                    if coordinated_plan
                    else ()
                ),
                idempotency_key=occurrence.occurrence_key,
            )
            with self._uow_factory() as uow:
                persisted = uow.company_operations.get_occurrence_by_key(
                    operation.id, occurrence.occurrence_key
                )
                if persisted is None:
                    raise CompanyOperationNotFound("Operation occurrence was not found")
                persisted.status = OccurrenceStatus.TASK_CREATED
                persisted.task_id = task.task.id
                persisted.updated_at = utc_now()
                uow.company_operations.save_occurrence(persisted)
                if operation.initiative_id is not None:
                    links = uow.company_goals.list_task_links(operation.initiative_id)
                    if not any(link.task_id == task.task.id for link in links):
                        uow.company_goals.add_task_link(
                            InitiativeTaskLink(
                                initiative_id=operation.initiative_id,
                                task_id=task.task.id,
                                created_by="company-operations",
                                created_at=utc_now(),
                            )
                        )
                self._emit(
                    uow,
                    "operation.task-created",
                    operation,
                    operation.company_id,
                    {
                        "occurrence_id": str(persisted.id),
                        "task_id": str(task.task.id),
                    },
                )
                uow.commit()
                return OperationLaunch(occurrence=persisted, task=task)
        except Exception as exc:
            with self._uow_factory() as uow:
                persisted = uow.company_operations.get_occurrence_by_key(
                    operation.id, occurrence.occurrence_key
                )
                if persisted is not None:
                    attempts = int(persisted.detail.get("attempts", 0)) + 1
                    retryable = attempts < 3
                    persisted.status = (
                        OccurrenceStatus.PENDING
                        if retryable
                        else OccurrenceStatus.FAILED
                    )
                    persisted.detail = {
                        "error": str(exc),
                        "attempts": attempts,
                        "retry_exhausted": not retryable,
                    }
                    persisted.updated_at = utc_now()
                    uow.company_operations.save_occurrence(persisted)
                uow.company_operations.add_exception(
                    OperationException.capture(
                        operation.id,
                        occurrence_id=occurrence.id,
                        code="TASK_CREATION_FAILED",
                        message=str(exc),
                        retryable=retryable,
                        next_retry_at=(
                            utc_now() + timedelta(minutes=5)
                            if retryable
                            else None
                        ),
                    )
                )
                uow.commit()
            raise

    def _appointed_plan(
        self, operation: CompanyOperation
    ) -> tuple[CoordinatedPlan, list[dict[str, str]]]:
        with self._uow_factory() as uow:
            workers = self._resolve_appointed_workers(uow, operation)
        if len(workers) < 2:
            raise InvalidCompanyOperation(
                "Staffed coordinated Operations require at least two appointed Positions"
            )
        specs = tuple(
            SubtaskSpec.create(
                key=position.key,
                objective=(
                    f"Act as {position.title}. Contribute your responsibility "
                    f"contract to this Operation: {operation.objective_template}"
                ),
                input={
                    "position_id": str(position.id),
                    "position_key": position.key,
                    "appointment_id": str(appointment.id),
                    "agent_version_id": str(version.id),
                    "responsibility_contract": dict(
                        position.responsibility_contract
                    ),
                },
                required_capabilities=position.required_capabilities,
                preferred_agent_id=definition.name,
            )
            for position, appointment, definition, version in workers
        )
        return (
            CoordinatedPlan.create(
                specs, max_concurrency=min(len(specs), 4)
            ),
            [
                {
                    "position_id": str(position.id),
                    "position_key": position.key,
                    "appointment_id": str(appointment.id),
                    "agent_definition_id": str(definition.id),
                    "agent_name": definition.name,
                    "agent_version_id": str(version.id),
                    "agent_version_digest": version.content_digest or "",
                }
                for position, appointment, definition, version in workers
            ],
        )

    def _resolve_appointed_workers(
        self, uow: Any, operation: CompanyOperation
    ) -> list[tuple[Any, Any, Any, Any]]:
        workers = []
        for position_id in operation.position_bindings:
            position = uow.company_model.get_position(position_id)
            if (
                position is None
                or position.company_id != operation.company_id
                or position.status is not ResourceStatus.ACTIVE
            ):
                raise InvalidCompanyOperation(
                    f"Position {position_id} is unavailable"
                )
            appointment = uow.company_model.get_active_appointment(position.id)
            if appointment is None:
                raise InvalidCompanyOperation(
                    f"Position '{position.key}' has no active Appointment"
                )
            definition = uow.agent_definitions.get(
                appointment.agent_definition_id
            )
            version = uow.agent_versions.get(appointment.agent_version_id)
            if (
                definition is None
                or definition.tenant_id != self._tenant_id
                or definition.lifecycle
                is not AgentDefinitionLifecycle.ACTIVE
                or definition.default_version_id != appointment.agent_version_id
                or version is None
                or version.status is not AgentVersionStatus.PUBLISHED
                or not version.content_digest
                or "async" not in version.execution_modes
            ):
                raise InvalidCompanyOperation(
                    f"Position '{position.key}' Appointment is stale"
                )
            missing = set(position.required_capabilities) - set(
                version.verified_capabilities
            )
            if missing:
                raise InvalidCompanyOperation(
                    f"Position '{position.key}' Agent lacks "
                    + ", ".join(sorted(missing))
                )
            workers.append((position, appointment, definition, version))
        return workers

    def _admit_occurrence(
        self, operation: CompanyOperation, occurrence: OperationOccurrence
    ) -> None:
        now = utc_now()
        with self._uow_factory() as uow:
            recent = uow.company_operations.count_occurrences(
                operation.id,
                since=now - timedelta(seconds=operation.window_seconds),
                statuses={OccurrenceStatus.TASK_CREATED},
            )
            if recent >= operation.maximum_runs_per_window:
                raise CompanyOperationConflict("Operation run window limit reached")
            active = 0
            for item in uow.company_operations.list_occurrences(
                operation.id, limit=operation.concurrency_limit + 100
            ):
                if item.task_id is None or item.id == occurrence.id:
                    continue
                task = uow.tasks.get(item.task_id)
                if task and task.status not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELED,
                }:
                    active += 1
            if active >= operation.concurrency_limit:
                raise CompanyOperationConflict("Operation concurrency limit reached")

    @staticmethod
    def _occurrence_key(operation: CompanyOperation, identity: str) -> str:
        normalized = identity.strip()
        if not normalized:
            raise InvalidCompanyOperation("Operation event ID is required")
        return (
            f"operation:{operation.id}:version:{operation.version}:"
            f"occurrence:{normalized}"
        )

    def _company(self, uow: Any, company_id: UUID):
        company = uow.company_model.get_company(company_id)
        if company is None or company.tenant_id != self._tenant_id:
            raise CompanyOperationNotFound(f"Company {company_id} was not found")
        return company

    def _active_company(self, uow: Any, company_id: UUID):
        company = self._company(uow, company_id)
        if company.status is not CompanyStatus.ACTIVE:
            raise CompanyOperationConflict("Archived Company cannot manage Operations")
        return company

    @staticmethod
    def _operation(
        uow: Any,
        company_id: UUID,
        operation_id: UUID,
        *,
        for_update: bool = False,
    ) -> CompanyOperation:
        operation = uow.company_operations.get_operation(
            operation_id, for_update=for_update
        )
        if operation is None or operation.company_id != company_id:
            raise CompanyOperationNotFound(f"Operation {operation_id} was not found")
        return operation

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.COMPANY_OPERATIONS)

    def _emit(
        self,
        uow: Any,
        suffix: str,
        operation: CompanyOperation,
        company_id: UUID,
        extra: dict[str, Any] | None = None,
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=f"agentmesh.company.{suffix}",
                tenant_id=self._tenant_id,
                aggregate_id=operation.id,
                payload={
                    "company_id": str(company_id),
                    "operation_id": str(operation.id),
                    "operation_version": operation.version,
                    "operation_digest": operation.content_digest,
                    **dict(extra or {}),
                },
            )
        )
