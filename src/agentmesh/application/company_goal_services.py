from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskAggregate, TaskApplicationService
from agentmesh.domain.company import CompanyStatus, ResourceStatus
from agentmesh.domain.company_goals import (
    CompanyObjective,
    Initiative,
    InitiativeStatus,
    InitiativeTaskLink,
    KeyResult,
    ObjectiveStatus,
    OperatingCycle,
    OperatingCycleStatus,
)
from agentmesh.domain.errors import (
    CompanyGoalConflict,
    CompanyGoalNotFound,
    InvalidCompanyGoal,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.tasks import TaskExecutionMode, TaskStatus, utc_now
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class ObjectiveSnapshot:
    objective: CompanyObjective
    key_results: list[KeyResult]
    initiatives: list[Initiative]
    task_links: dict[UUID, list[InitiativeTaskLink]]


@dataclass(frozen=True)
class CycleSnapshot:
    cycle: OperatingCycle
    objectives: list[ObjectiveSnapshot]


@dataclass(frozen=True)
class InitiativeTaskLaunch:
    task: TaskAggregate
    link: InitiativeTaskLink


class CompanyGoalService:
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

    def create_cycle(
        self,
        company_id: UUID,
        *,
        name: str,
        starts_at: datetime,
        ends_at: datetime,
        review_schedule: dict[str, Any] | None = None,
    ) -> OperatingCycle:
        self._require_enabled()
        cycle = OperatingCycle.create(
            company_id=company_id,
            name=name,
            starts_at=starts_at,
            ends_at=ends_at,
            review_schedule=review_schedule,
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            uow.company_goals.add_cycle(cycle)
            self._emit(uow, "operating-cycle.created", cycle.id, company_id)
            uow.commit()
        return cycle

    def transition_cycle(
        self, company_id: UUID, cycle_id: UUID, action: str, actor: str
    ) -> OperatingCycle:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            cycle = self._cycle(uow, company_id, cycle_id, for_update=True)
            if action == "approve":
                cycle.approve(actor)
            elif action == "activate":
                active = uow.company_goals.get_active_cycle(company_id)
                if active is not None and active.id != cycle.id:
                    raise CompanyGoalConflict("Company already has an active Operating Cycle")
                cycle.activate()
            elif action == "pause":
                cycle.pause()
            elif action == "review":
                cycle.review()
            elif action == "close":
                cycle.close()
            else:
                raise InvalidCompanyGoal(f"Unknown Operating Cycle action '{action}'")
            uow.company_goals.save_cycle(cycle)
            event = {
                "approve": "approved",
                "activate": "activated",
                "pause": "paused",
                "review": "reviewing",
                "close": "closed",
            }[action]
            self._emit(uow, f"operating-cycle.{event}", cycle.id, company_id)
            uow.commit()
            return cycle

    def get_cycle(self, company_id: UUID, cycle_id: UUID) -> CycleSnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            cycle = self._cycle(uow, company_id, cycle_id)
            objectives = uow.company_goals.list_objectives(cycle.id)
            return CycleSnapshot(
                cycle=cycle,
                objectives=[
                    ObjectiveSnapshot(
                        objective=objective,
                        key_results=uow.company_goals.list_key_results(objective.id),
                        initiatives=uow.company_goals.list_initiatives(objective.id),
                        task_links={
                            initiative.id: uow.company_goals.list_task_links(initiative.id)
                            for initiative in uow.company_goals.list_initiatives(objective.id)
                        },
                    )
                    for objective in objectives
                ],
            )

    def list_cycles(self, company_id: UUID) -> list[OperatingCycle]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.company_goals.list_cycles(company_id)

    def create_objective(
        self,
        company_id: UUID,
        cycle_id: UUID,
        **values: Any,
    ) -> CompanyObjective:
        self._require_enabled()
        objective = CompanyObjective.create(
            company_id=company_id,
            cycle_id=cycle_id,
            **values,
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            cycle = self._cycle(uow, company_id, cycle_id)
            if cycle.status not in {
                OperatingCycleStatus.DRAFT,
                OperatingCycleStatus.APPROVED,
                OperatingCycleStatus.ACTIVE,
            }:
                raise CompanyGoalConflict("Operating Cycle does not accept new Objectives")
            if not cycle.starts_at <= objective.target_date <= cycle.ends_at:
                raise InvalidCompanyGoal(
                    "Objective target date must fall within the Operating Cycle"
                )
            position = uow.company_model.get_position(objective.owner_position_id)
            if (
                position is None
                or position.company_id != company_id
                or position.status is not ResourceStatus.ACTIVE
            ):
                raise InvalidCompanyGoal("Objective owner must be an active Company Position")
            uow.company_goals.add_objective(objective)
            self._emit(uow, "objective.created", objective.id, company_id)
            uow.commit()
        return objective

    def transition_objective(
        self, company_id: UUID, objective_id: UUID, action: str
    ) -> CompanyObjective:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            objective = self._objective(uow, company_id, objective_id, for_update=True)
            if action == "approve":
                objective.approve()
            elif action == "activate":
                cycle = self._cycle(uow, company_id, objective.cycle_id)
                if cycle.status is not OperatingCycleStatus.ACTIVE:
                    raise CompanyGoalConflict("Objective requires an active Operating Cycle")
                objective.activate()
            elif action == "achieve":
                objective.achieve()
            else:
                raise InvalidCompanyGoal(f"Unknown Objective action '{action}'")
            uow.company_goals.save_objective(objective)
            self._emit(uow, f"objective.{action}d", objective.id, company_id)
            uow.commit()
            return objective

    def create_key_result(
        self, company_id: UUID, objective_id: UUID, **values: Any
    ) -> KeyResult:
        self._require_enabled()
        key_result = KeyResult.create(
            company_id=company_id, objective_id=objective_id, **values
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            objective = self._objective(uow, company_id, objective_id)
            if any(
                item.metric_key == key_result.metric_key
                for item in uow.company_goals.list_key_results(objective.id)
            ):
                raise CompanyGoalConflict("Objective already contains this Key Result metric")
            uow.company_goals.add_key_result(key_result)
            self._emit(uow, "key-result.created", key_result.id, company_id)
            uow.commit()
        return key_result

    def record_key_result(
        self,
        company_id: UUID,
        key_result_id: UUID,
        *,
        value: str,
        verified: bool,
        source: str | None = None,
    ) -> KeyResult:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            result = uow.company_goals.get_key_result(key_result_id, for_update=True)
            if result is None or result.company_id != company_id:
                raise CompanyGoalNotFound(f"Key Result {key_result_id} was not found")
            if verified:
                if source is None:
                    raise InvalidCompanyGoal("Verified Key Result measurement requires a source")
                result.record_verified(value, source)
            else:
                result.record_estimate(value)
            uow.company_goals.save_key_result(result)
            self._emit(
                uow,
                "key-result.measured",
                result.id,
                company_id,
                {"verification": "VERIFIED" if verified else "ESTIMATED"},
            )
            uow.commit()
            return result

    def create_initiative(
        self, company_id: UUID, objective_id: UUID, **values: Any
    ) -> Initiative:
        self._require_enabled()
        initiative = Initiative.create(
            company_id=company_id, objective_id=objective_id, **values
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            self._objective(uow, company_id, objective_id)
            unit = uow.company_model.get_unit(initiative.owner_unit_id)
            if (
                unit is None
                or unit.company_id != company_id
                or unit.status is not ResourceStatus.ACTIVE
            ):
                raise InvalidCompanyGoal("Initiative owner must be an active Organization Unit")
            uow.company_goals.add_initiative(initiative)
            self._emit(uow, "initiative.created", initiative.id, company_id)
            uow.commit()
        return initiative

    def transition_initiative(
        self, company_id: UUID, initiative_id: UUID, action: str
    ) -> Initiative:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            initiative = self._initiative(uow, company_id, initiative_id, for_update=True)
            if action == "approve":
                initiative.approve()
            elif action == "activate":
                objective = self._objective(uow, company_id, initiative.objective_id)
                if objective.status is not ObjectiveStatus.ACTIVE:
                    raise CompanyGoalConflict("Initiative requires an active Objective")
                initiative.activate()
            elif action == "complete":
                links = uow.company_goals.list_task_links(initiative.id)
                completed = [
                    task
                    for link in links
                    if (task := uow.tasks.get(link.task_id)) is not None
                    and task.status is TaskStatus.COMPLETED
                ]
                if not completed:
                    raise CompanyGoalConflict(
                        "Initiative requires completed Task evidence before completion"
                    )
                initiative.complete()
            else:
                raise InvalidCompanyGoal(f"Unknown Initiative action '{action}'")
            uow.company_goals.save_initiative(initiative)
            self._emit(uow, f"initiative.{action}d", initiative.id, company_id)
            uow.commit()
            return initiative

    def launch_task(
        self,
        company_id: UUID,
        initiative_id: UUID,
        *,
        objective: str,
        input: dict[str, Any] | None,
        created_by: str,
    ) -> InitiativeTaskLaunch:
        self._require_enabled()
        actor = created_by.strip()
        if not actor:
            raise InvalidCompanyGoal("Task creator is required")
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            initiative = self._initiative(uow, company_id, initiative_id)
            if initiative.status is not InitiativeStatus.ACTIVE:
                raise CompanyGoalConflict("Only an active Initiative can launch Tasks")
        enriched_input = {
            **dict(input or {}),
            "company_context": {
                "company_id": str(company_id),
                "initiative_id": str(initiative_id),
            },
        }
        task = self._task_service.create_task(
            objective,
            input=enriched_input,
            execution_mode=TaskExecutionMode.DIRECT,
        )
        link = InitiativeTaskLink(
            initiative_id=initiative_id,
            task_id=task.task.id,
            created_by=actor,
            created_at=utc_now(),
        )
        with self._uow_factory() as uow:
            self._initiative(uow, company_id, initiative_id)
            uow.company_goals.add_task_link(link)
            self._emit(
                uow,
                "initiative.task-linked",
                initiative_id,
                company_id,
                {"task_id": str(task.task.id)},
            )
            uow.commit()
        return InitiativeTaskLaunch(task=task, link=link)

    def _company(self, uow: Any, company_id: UUID):
        company = uow.company_model.get_company(company_id)
        if company is None or company.tenant_id != self._tenant_id:
            raise CompanyGoalNotFound(f"Company {company_id} was not found")
        return company

    def _active_company(self, uow: Any, company_id: UUID):
        company = self._company(uow, company_id)
        if company.status is not CompanyStatus.ACTIVE:
            raise CompanyGoalConflict("Archived Company cannot manage goals")
        return company

    @staticmethod
    def _cycle(
        uow: Any, company_id: UUID, cycle_id: UUID, *, for_update: bool = False
    ) -> OperatingCycle:
        cycle = uow.company_goals.get_cycle(cycle_id, for_update=for_update)
        if cycle is None or cycle.company_id != company_id:
            raise CompanyGoalNotFound(f"Operating Cycle {cycle_id} was not found")
        return cycle

    @staticmethod
    def _objective(
        uow: Any, company_id: UUID, objective_id: UUID, *, for_update: bool = False
    ) -> CompanyObjective:
        objective = uow.company_goals.get_objective(objective_id, for_update=for_update)
        if objective is None or objective.company_id != company_id:
            raise CompanyGoalNotFound(f"Objective {objective_id} was not found")
        return objective

    @staticmethod
    def _initiative(
        uow: Any, company_id: UUID, initiative_id: UUID, *, for_update: bool = False
    ) -> Initiative:
        initiative = uow.company_goals.get_initiative(
            initiative_id, for_update=for_update
        )
        if initiative is None or initiative.company_id != company_id:
            raise CompanyGoalNotFound(f"Initiative {initiative_id} was not found")
        return initiative

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.COMPANY_GOALS)

    def _emit(
        self,
        uow: Any,
        suffix: str,
        aggregate_id: UUID,
        company_id: UUID,
        extra: dict[str, Any] | None = None,
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=f"agentmesh.company.{suffix}",
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"company_id": str(company_id), **dict(extra or {})},
            )
        )
