from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.model_connections import ModelConnection
from agentmesh.infrastructure.postgres.models import ModelConnectionRecord


class SqlAlchemyModelConnectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, value: ModelConnection) -> None:
        self._session.add(self._record(value))

    def get(
        self, tenant_id: str, connection_id: UUID, *, for_update: bool = False
    ) -> ModelConnection | None:
        query = select(ModelConnectionRecord).where(
            ModelConnectionRecord.tenant_id == tenant_id,
            ModelConnectionRecord.id == connection_id,
        )
        if for_update:
            query = query.with_for_update()
        record = self._session.scalar(query)
        return self._domain(record) if record else None

    def get_by_name(self, tenant_id: str, name: str) -> ModelConnection | None:
        record = self._session.scalar(
            select(ModelConnectionRecord).where(
                ModelConnectionRecord.tenant_id == tenant_id,
                ModelConnectionRecord.name == name,
            )
        )
        return self._domain(record) if record else None

    def list(self, tenant_id: str) -> list[ModelConnection]:
        records = self._session.scalars(
            select(ModelConnectionRecord)
            .where(
                ModelConnectionRecord.tenant_id == tenant_id,
            )
            .order_by(ModelConnectionRecord.created_at.desc())
        )
        return [self._domain(record) for record in records]

    def save(self, value: ModelConnection) -> None:
        record = self._session.get(ModelConnectionRecord, value.id)
        if record is None or record.tenant_id != value.tenant_id:
            raise LookupError(value.id)
        for key, item in self._record(value).__dict__.items():
            if key != "_sa_instance_state":
                setattr(record, key, item)

    @staticmethod
    def _record(value: ModelConnection) -> ModelConnectionRecord:
        return ModelConnectionRecord(**value.__dict__)

    @staticmethod
    def _domain(record: ModelConnectionRecord) -> ModelConnection:
        return ModelConnection(
            **{key: getattr(record, key) for key in ModelConnection.__dataclass_fields__}
        )
