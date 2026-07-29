from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.company_packs import (
    CompanyPack,
    PackInstallation,
    PackKind,
    PackStatus,
)
from agentmesh.infrastructure.postgres.models import (
    CompanyPackInstallationRecord,
    CompanyPackRecord,
)


class SqlAlchemyCompanyPackRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_pack(self, value: CompanyPack) -> None:
        self._session.add(CompanyPackRecord(**self._pack_data(value)))

    def get_pack(self, pack_id: UUID) -> CompanyPack | None:
        record = self._session.get(CompanyPackRecord, pack_id)
        return self._pack(record) if record else None

    def get_pack_by_key_version(self, key: str, version: str) -> CompanyPack | None:
        record = self._session.scalar(
            select(CompanyPackRecord).where(
                CompanyPackRecord.key == key, CompanyPackRecord.version == version
            )
        )
        return self._pack(record) if record else None

    def list_packs(self) -> list[CompanyPack]:
        records = self._session.scalars(
            select(CompanyPackRecord).order_by(
                CompanyPackRecord.key, CompanyPackRecord.version
            )
        )
        return [self._pack(record) for record in records]

    def save_pack(self, value: CompanyPack) -> None:
        record = self._session.get(CompanyPackRecord, value.id)
        if record is None:
            raise LookupError(f"CompanyPackRecord {value.id} was not found")
        record.status = value.status.value
        record.published_at = value.published_at

    def add_installation(self, value: PackInstallation) -> None:
        self._session.add(CompanyPackInstallationRecord(**value.__dict__))

    def get_installation(
        self, company_id: UUID, pack_key: str
    ) -> PackInstallation | None:
        record = self._session.scalar(
            select(CompanyPackInstallationRecord).where(
                CompanyPackInstallationRecord.company_id == company_id,
                CompanyPackInstallationRecord.pack_key == pack_key,
            )
        )
        return self._installation(record) if record else None

    def list_installations(self, company_id: UUID) -> list[PackInstallation]:
        records = self._session.scalars(
            select(CompanyPackInstallationRecord)
            .where(CompanyPackInstallationRecord.company_id == company_id)
            .order_by(CompanyPackInstallationRecord.installed_at)
        )
        return [self._installation(record) for record in records]

    @staticmethod
    def _pack_data(value: CompanyPack) -> dict:
        data = dict(value.__dict__)
        data["kind"] = value.kind.value
        data["status"] = value.status.value
        return data

    @classmethod
    def _pack(cls, record: CompanyPackRecord) -> CompanyPack:
        data = cls._record_data(record)
        data["kind"] = PackKind(data["kind"])
        data["status"] = PackStatus(data["status"])
        return CompanyPack(**data)

    @classmethod
    def _installation(
        cls, record: CompanyPackInstallationRecord
    ) -> PackInstallation:
        return PackInstallation(**cls._record_data(record))

    @staticmethod
    def _record_data(record) -> dict:
        return {
            column.name: getattr(record, column.name)
            for column in record.__table__.columns
        }
