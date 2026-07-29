from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.business_objects import (
    BusinessObject,
    BusinessObjectRevision,
    BusinessObjectType,
    BusinessObjectTypeStatus,
    ObjectSourceType,
)
from agentmesh.infrastructure.postgres.models import (
    BusinessObjectRecord,
    BusinessObjectRevisionRecord,
    BusinessObjectTypeRecord,
)


class SqlAlchemyBusinessObjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_type(self, value: BusinessObjectType) -> None:
        self._session.add(BusinessObjectTypeRecord(**self._type_data(value)))

    def get_type(
        self, type_id: UUID, *, for_update: bool = False
    ) -> BusinessObjectType | None:
        record = self._session.get(
            BusinessObjectTypeRecord, type_id, with_for_update=for_update
        )
        return self._type(record) if record else None

    def get_type_by_key(
        self,
        company_id: UUID,
        key: str,
        *,
        schema_version: int | None = None,
        published_only: bool = False,
    ) -> BusinessObjectType | None:
        query = select(BusinessObjectTypeRecord).where(
            BusinessObjectTypeRecord.company_id == company_id,
            BusinessObjectTypeRecord.key == key,
        )
        if schema_version is not None:
            query = query.where(
                BusinessObjectTypeRecord.schema_version == schema_version
            )
        if published_only:
            query = query.where(
                BusinessObjectTypeRecord.status
                == BusinessObjectTypeStatus.PUBLISHED.value
            )
        record = self._session.scalar(
            query.order_by(BusinessObjectTypeRecord.schema_version.desc())
        )
        return self._type(record) if record else None

    def list_types(self, company_id: UUID) -> list[BusinessObjectType]:
        records = self._session.scalars(
            select(BusinessObjectTypeRecord)
            .where(BusinessObjectTypeRecord.company_id == company_id)
            .order_by(
                BusinessObjectTypeRecord.key,
                BusinessObjectTypeRecord.schema_version.desc(),
            )
        )
        return [self._type(record) for record in records]

    def save_type(self, value: BusinessObjectType) -> None:
        record = self._required(BusinessObjectTypeRecord, value.id)
        for key, item in self._type_data(value).items():
            if key != "id":
                setattr(record, key, item)

    def add_object(self, value: BusinessObject) -> None:
        self._session.add(BusinessObjectRecord(**value.__dict__))

    def get_object(
        self, object_id: UUID, *, for_update: bool = False
    ) -> BusinessObject | None:
        record = self._session.get(
            BusinessObjectRecord, object_id, with_for_update=for_update
        )
        return self._object(record) if record else None

    def get_object_by_external_ref(
        self, type_id: UUID, external_ref: str
    ) -> BusinessObject | None:
        record = self._session.scalar(
            select(BusinessObjectRecord).where(
                BusinessObjectRecord.type_id == type_id,
                BusinessObjectRecord.external_ref == external_ref,
            )
        )
        return self._object(record) if record else None

    def list_objects(
        self,
        company_id: UUID,
        *,
        type_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[BusinessObject]:
        query = select(BusinessObjectRecord).where(
            BusinessObjectRecord.company_id == company_id
        )
        if type_id is not None:
            query = query.where(BusinessObjectRecord.type_id == type_id)
        records = self._session.scalars(
            query.order_by(BusinessObjectRecord.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [self._object(record) for record in records]

    def save_object(self, value: BusinessObject) -> None:
        record = self._required(BusinessObjectRecord, value.id)
        for key, item in value.__dict__.items():
            if key not in {"id", "company_id", "type_id", "created_at"}:
                setattr(record, key, item)

    def add_revision(self, value: BusinessObjectRevision) -> None:
        data = dict(value.__dict__)
        data["source_type"] = value.source_type.value
        self._session.add(BusinessObjectRevisionRecord(**data))

    def get_revision(
        self, object_id: UUID, revision: int
    ) -> BusinessObjectRevision | None:
        record = self._session.get(
            BusinessObjectRevisionRecord, (object_id, revision)
        )
        return self._revision(record) if record else None

    def list_revisions(self, object_id: UUID) -> list[BusinessObjectRevision]:
        records = self._session.scalars(
            select(BusinessObjectRevisionRecord)
            .where(BusinessObjectRevisionRecord.object_id == object_id)
            .order_by(BusinessObjectRevisionRecord.revision)
        )
        return [self._revision(record) for record in records]

    @staticmethod
    def _type_data(value: BusinessObjectType) -> dict:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        return data

    @staticmethod
    def _record_data(record) -> dict:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        return data

    @classmethod
    def _type(cls, record: BusinessObjectTypeRecord) -> BusinessObjectType:
        data = cls._record_data(record)
        data["status"] = BusinessObjectTypeStatus(record.status)
        return BusinessObjectType(**data)

    @classmethod
    def _object(cls, record: BusinessObjectRecord) -> BusinessObject:
        return BusinessObject(**cls._record_data(record))

    @classmethod
    def _revision(
        cls, record: BusinessObjectRevisionRecord
    ) -> BusinessObjectRevision:
        data = cls._record_data(record)
        data["source_type"] = ObjectSourceType(record.source_type)
        return BusinessObjectRevision(**data)

    def _required(self, model, record_id):
        record = self._session.get(model, record_id)
        if record is None:
            raise LookupError(record_id)
        return record
