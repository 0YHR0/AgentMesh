from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentmesh.domain.company_operations import (
    CompanyOperation,
    MissedSchedulePolicy,
    OccurrenceStatus,
    OperationException,
    OperationOccurrence,
    OperationStatus,
    OperationTriggerState,
    TriggerKind,
)
from agentmesh.infrastructure.postgres.models import (
    CompanyOperationExceptionRecord,
    CompanyOperationOccurrenceRecord,
    CompanyOperationRecord,
    CompanyOperationTriggerStateRecord,
    CompanyRecord,
)


class SqlAlchemyCompanyOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_operation(self, value: CompanyOperation) -> None:
        self._session.add(CompanyOperationRecord(**self._operation_data(value)))

    def get_operation(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> CompanyOperation | None:
        record = self._session.get(
            CompanyOperationRecord, operation_id, with_for_update=for_update
        )
        return self._operation(record) if record else None

    def get_operation_by_key(
        self, company_id: UUID, key: str
    ) -> CompanyOperation | None:
        record = self._session.scalar(
            select(CompanyOperationRecord).where(
                CompanyOperationRecord.company_id == company_id,
                CompanyOperationRecord.key == key,
            )
        )
        return self._operation(record) if record else None

    def list_operations(self, company_id: UUID) -> list[CompanyOperation]:
        records = self._session.scalars(
            select(CompanyOperationRecord)
            .where(CompanyOperationRecord.company_id == company_id)
            .order_by(CompanyOperationRecord.created_at)
        )
        return [self._operation(record) for record in records]

    def save_operation(self, value: CompanyOperation) -> None:
        record = self._required(CompanyOperationRecord, value.id)
        for key, item in self._operation_data(value).items():
            if key != "id":
                setattr(record, key, item)

    def add_trigger_state(self, value: OperationTriggerState) -> None:
        self._session.add(CompanyOperationTriggerStateRecord(**value.__dict__))

    def get_trigger_state(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> OperationTriggerState | None:
        record = self._session.get(
            CompanyOperationTriggerStateRecord,
            operation_id,
            with_for_update=for_update,
        )
        return self._state(record) if record else None

    def list_due(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationTriggerState]]:
        records = self._session.execute(
            select(CompanyOperationRecord, CompanyOperationTriggerStateRecord)
            .join(
                CompanyRecord,
                CompanyRecord.id == CompanyOperationRecord.company_id,
            )
            .join(
                CompanyOperationTriggerStateRecord,
                CompanyOperationTriggerStateRecord.operation_id
                == CompanyOperationRecord.id,
            )
            .where(
                CompanyOperationRecord.status == OperationStatus.ACTIVE.value,
                CompanyRecord.tenant_id == tenant_id,
                CompanyOperationTriggerStateRecord.next_due_at.is_not(None),
                CompanyOperationTriggerStateRecord.next_due_at <= now,
            )
            .order_by(CompanyOperationTriggerStateRecord.next_due_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=CompanyOperationTriggerStateRecord)
        )
        return [
            (self._operation(operation), self._state(state))
            for operation, state in records
        ]

    def save_trigger_state(self, value: OperationTriggerState) -> None:
        record = self._required(CompanyOperationTriggerStateRecord, value.operation_id)
        for key, item in value.__dict__.items():
            if key != "operation_id":
                setattr(record, key, item)

    def add_occurrence(self, value: OperationOccurrence) -> None:
        self._session.add(
            CompanyOperationOccurrenceRecord(**self._occurrence_data(value))
        )

    def get_occurrence_by_key(
        self, operation_id: UUID, occurrence_key: str
    ) -> OperationOccurrence | None:
        record = self._session.scalar(
            select(CompanyOperationOccurrenceRecord).where(
                CompanyOperationOccurrenceRecord.operation_id == operation_id,
                CompanyOperationOccurrenceRecord.occurrence_key == occurrence_key,
            )
        )
        return self._occurrence(record) if record else None

    def list_occurrences(
        self, operation_id: UUID, *, limit: int = 100
    ) -> list[OperationOccurrence]:
        records = self._session.scalars(
            select(CompanyOperationOccurrenceRecord)
            .where(CompanyOperationOccurrenceRecord.operation_id == operation_id)
            .order_by(CompanyOperationOccurrenceRecord.scheduled_at.desc())
            .limit(limit)
        )
        return [self._occurrence(record) for record in records]

    def count_occurrences(
        self,
        operation_id: UUID,
        *,
        since: datetime,
        statuses: set[OccurrenceStatus],
    ) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(CompanyOperationOccurrenceRecord)
                .where(
                    CompanyOperationOccurrenceRecord.operation_id == operation_id,
                    CompanyOperationOccurrenceRecord.created_at >= since,
                    CompanyOperationOccurrenceRecord.status.in_(
                        [status.value for status in statuses]
                    ),
                )
            )
            or 0
        )

    def save_occurrence(self, value: OperationOccurrence) -> None:
        record = self._required(CompanyOperationOccurrenceRecord, value.id)
        for key, item in self._occurrence_data(value).items():
            if key != "id":
                setattr(record, key, item)

    def add_exception(self, value: OperationException) -> None:
        self._session.add(CompanyOperationExceptionRecord(**value.__dict__))

    def list_exceptions(
        self, operation_id: UUID, *, unresolved_only: bool = False
    ) -> list[OperationException]:
        query = select(CompanyOperationExceptionRecord).where(
            CompanyOperationExceptionRecord.operation_id == operation_id
        )
        if unresolved_only:
            query = query.where(CompanyOperationExceptionRecord.resolved_at.is_(None))
        records = self._session.scalars(
            query.order_by(CompanyOperationExceptionRecord.created_at.desc())
        )
        return [self._exception(record) for record in records]

    def list_retryable(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationOccurrence, OperationException]]:
        records = self._session.execute(
            select(
                CompanyOperationRecord,
                CompanyOperationOccurrenceRecord,
                CompanyOperationExceptionRecord,
            )
            .join(
                CompanyRecord,
                CompanyRecord.id == CompanyOperationRecord.company_id,
            )
            .join(
                CompanyOperationOccurrenceRecord,
                CompanyOperationOccurrenceRecord.operation_id
                == CompanyOperationRecord.id,
            )
            .join(
                CompanyOperationExceptionRecord,
                CompanyOperationExceptionRecord.occurrence_id
                == CompanyOperationOccurrenceRecord.id,
            )
            .where(
                CompanyOperationRecord.status == OperationStatus.ACTIVE.value,
                CompanyRecord.tenant_id == tenant_id,
                CompanyOperationOccurrenceRecord.status
                == OccurrenceStatus.PENDING.value,
                CompanyOperationExceptionRecord.retryable.is_(True),
                CompanyOperationExceptionRecord.resolved_at.is_(None),
                CompanyOperationExceptionRecord.next_retry_at <= now,
            )
            .order_by(CompanyOperationExceptionRecord.next_retry_at)
            .limit(limit)
            .with_for_update(
                skip_locked=True, of=CompanyOperationOccurrenceRecord
            )
        )
        return [
            (
                self._operation(operation),
                self._occurrence(occurrence),
                self._exception(exception),
            )
            for operation, occurrence, exception in records
        ]

    def save_exception(self, value: OperationException) -> None:
        record = self._required(CompanyOperationExceptionRecord, value.id)
        for key, item in value.__dict__.items():
            if key != "id":
                setattr(record, key, item)

    @staticmethod
    def _operation_data(value: CompanyOperation) -> dict:
        data = dict(value.__dict__)
        data["trigger_kind"] = value.trigger_kind.value
        data["missed_policy"] = value.missed_policy.value
        data["status"] = value.status.value
        data["position_bindings"] = [str(item) for item in value.position_bindings]
        return data

    @staticmethod
    def _occurrence_data(value: OperationOccurrence) -> dict:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        return data

    @staticmethod
    def _record_data(record) -> dict:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        return data

    @classmethod
    def _operation(cls, record: CompanyOperationRecord) -> CompanyOperation:
        data = cls._record_data(record)
        data["trigger_kind"] = TriggerKind(record.trigger_kind)
        data["missed_policy"] = MissedSchedulePolicy(record.missed_policy)
        data["status"] = OperationStatus(record.status)
        data["position_bindings"] = [UUID(item) for item in record.position_bindings]
        return CompanyOperation(**data)

    @classmethod
    def _state(
        cls, record: CompanyOperationTriggerStateRecord
    ) -> OperationTriggerState:
        return OperationTriggerState(**cls._record_data(record))

    @classmethod
    def _occurrence(
        cls, record: CompanyOperationOccurrenceRecord
    ) -> OperationOccurrence:
        data = cls._record_data(record)
        data["status"] = OccurrenceStatus(record.status)
        return OperationOccurrence(**data)

    @classmethod
    def _exception(
        cls, record: CompanyOperationExceptionRecord
    ) -> OperationException:
        return OperationException(**cls._record_data(record))

    def _required(self, model, record_id: UUID):
        record = self._session.get(model, record_id)
        if record is None:
            raise LookupError(record_id)
        return record
