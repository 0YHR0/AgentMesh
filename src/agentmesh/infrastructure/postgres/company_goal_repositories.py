from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.company_goals import (
    CompanyObjective,
    Initiative,
    InitiativeStatus,
    InitiativeTaskLink,
    KeyResult,
    KeyResultStatus,
    ObjectiveStatus,
    OperatingCycle,
    OperatingCycleStatus,
)
from agentmesh.infrastructure.postgres.models import (
    CompanyInitiativeRecord,
    CompanyKeyResultRecord,
    CompanyObjectiveRecord,
    InitiativeTaskLinkRecord,
    OperatingCycleRecord,
)


class SqlAlchemyCompanyGoalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_cycle(self, value: OperatingCycle) -> None:
        self._session.add(OperatingCycleRecord(**self._data(value)))

    def get_cycle(
        self, cycle_id: UUID, *, for_update: bool = False
    ) -> OperatingCycle | None:
        record = self._session.get(OperatingCycleRecord, cycle_id, with_for_update=for_update)
        return self._cycle(record) if record else None

    def get_active_cycle(self, company_id: UUID) -> OperatingCycle | None:
        record = self._session.scalar(
            select(OperatingCycleRecord).where(
                OperatingCycleRecord.company_id == company_id,
                OperatingCycleRecord.status == OperatingCycleStatus.ACTIVE.value,
            )
        )
        return self._cycle(record) if record else None

    def list_cycles(self, company_id: UUID) -> list[OperatingCycle]:
        records = self._session.scalars(
            select(OperatingCycleRecord)
            .where(OperatingCycleRecord.company_id == company_id)
            .order_by(OperatingCycleRecord.created_at)
        )
        return [self._cycle(record) for record in records]

    def save_cycle(self, value: OperatingCycle) -> None:
        self._copy(self._required(OperatingCycleRecord, value.id), value)

    def add_objective(self, value: CompanyObjective) -> None:
        self._session.add(CompanyObjectiveRecord(**self._data(value)))

    def get_objective(
        self, objective_id: UUID, *, for_update: bool = False
    ) -> CompanyObjective | None:
        record = self._session.get(
            CompanyObjectiveRecord, objective_id, with_for_update=for_update
        )
        return self._objective(record) if record else None

    def list_objectives(self, cycle_id: UUID) -> list[CompanyObjective]:
        records = self._session.scalars(
            select(CompanyObjectiveRecord)
            .where(CompanyObjectiveRecord.cycle_id == cycle_id)
            .order_by(CompanyObjectiveRecord.priority, CompanyObjectiveRecord.created_at)
        )
        return [self._objective(record) for record in records]

    def save_objective(self, value: CompanyObjective) -> None:
        self._copy(self._required(CompanyObjectiveRecord, value.id), value)

    def add_key_result(self, value: KeyResult) -> None:
        self._session.add(CompanyKeyResultRecord(**self._data(value)))

    def get_key_result(
        self, key_result_id: UUID, *, for_update: bool = False
    ) -> KeyResult | None:
        record = self._session.get(
            CompanyKeyResultRecord, key_result_id, with_for_update=for_update
        )
        return self._key_result(record) if record else None

    def list_key_results(self, objective_id: UUID) -> list[KeyResult]:
        records = self._session.scalars(
            select(CompanyKeyResultRecord)
            .where(CompanyKeyResultRecord.objective_id == objective_id)
            .order_by(CompanyKeyResultRecord.created_at)
        )
        return [self._key_result(record) for record in records]

    def save_key_result(self, value: KeyResult) -> None:
        self._copy(self._required(CompanyKeyResultRecord, value.id), value)

    def add_initiative(self, value: Initiative) -> None:
        self._session.add(CompanyInitiativeRecord(**self._data(value)))

    def get_initiative(
        self, initiative_id: UUID, *, for_update: bool = False
    ) -> Initiative | None:
        record = self._session.get(
            CompanyInitiativeRecord, initiative_id, with_for_update=for_update
        )
        return self._initiative(record) if record else None

    def list_initiatives(self, objective_id: UUID) -> list[Initiative]:
        records = self._session.scalars(
            select(CompanyInitiativeRecord)
            .where(CompanyInitiativeRecord.objective_id == objective_id)
            .order_by(CompanyInitiativeRecord.created_at)
        )
        return [self._initiative(record) for record in records]

    def save_initiative(self, value: Initiative) -> None:
        self._copy(self._required(CompanyInitiativeRecord, value.id), value)

    def add_task_link(self, value: InitiativeTaskLink) -> None:
        self._session.add(InitiativeTaskLinkRecord(**value.__dict__))

    def list_task_links(self, initiative_id: UUID) -> list[InitiativeTaskLink]:
        records = self._session.scalars(
            select(InitiativeTaskLinkRecord)
            .where(InitiativeTaskLinkRecord.initiative_id == initiative_id)
            .order_by(InitiativeTaskLinkRecord.created_at)
        )
        return [
            InitiativeTaskLink(
                initiative_id=record.initiative_id,
                task_id=record.task_id,
                created_by=record.created_by,
                created_at=record.created_at,
            )
            for record in records
        ]

    @staticmethod
    def _data(value) -> dict:
        data = dict(value.__dict__)
        if hasattr(value.status, "value"):
            data["status"] = value.status.value
        return data

    @staticmethod
    def _copy(record, value) -> None:
        for key, item in SqlAlchemyCompanyGoalRepository._data(value).items():
            if key not in {"id", "company_id", "cycle_id", "objective_id"}:
                setattr(record, key, item)

    def _required(self, model, record_id: UUID):
        record = self._session.get(model, record_id)
        if record is None:
            raise LookupError(record_id)
        return record

    @staticmethod
    def _record_data(record) -> dict:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        return data

    @classmethod
    def _cycle(cls, record: OperatingCycleRecord) -> OperatingCycle:
        data = cls._record_data(record)
        data["status"] = OperatingCycleStatus(record.status)
        return OperatingCycle(**data)

    @classmethod
    def _objective(cls, record: CompanyObjectiveRecord) -> CompanyObjective:
        data = cls._record_data(record)
        data["status"] = ObjectiveStatus(record.status)
        return CompanyObjective(**data)

    @classmethod
    def _key_result(cls, record: CompanyKeyResultRecord) -> KeyResult:
        data = cls._record_data(record)
        data["status"] = KeyResultStatus(record.status)
        return KeyResult(**data)

    @classmethod
    def _initiative(cls, record: CompanyInitiativeRecord) -> Initiative:
        data = cls._record_data(record)
        data["status"] = InitiativeStatus(record.status)
        return Initiative(**data)
