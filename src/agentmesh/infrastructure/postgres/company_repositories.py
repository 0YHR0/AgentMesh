from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.company import (
    Appointment,
    AppointmentStatus,
    Company,
    CompanyStatus,
    OrganizationNodeType,
    OrganizationRelationship,
    OrganizationUnit,
    Position,
    ResourceStatus,
)
from agentmesh.infrastructure.postgres.models import (
    AppointmentRecord,
    CompanyPositionRecord,
    CompanyRecord,
    OrganizationRelationshipRecord,
    OrganizationUnitRecord,
)


class SqlAlchemyCompanyModelRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_company(self, value: Company) -> None:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        self._session.add(CompanyRecord(**data))

    def get_company(self, company_id: UUID, *, for_update: bool = False) -> Company | None:
        record = self._session.get(CompanyRecord, company_id, with_for_update=for_update)
        return self._company(record) if record else None

    def get_active_company(self, tenant_id: str) -> Company | None:
        record = self._session.scalar(
            select(CompanyRecord).where(
                CompanyRecord.tenant_id == tenant_id,
                CompanyRecord.status == CompanyStatus.ACTIVE.value,
            )
        )
        return self._company(record) if record else None

    def list_companies(self, tenant_id: str) -> list[Company]:
        records = self._session.scalars(
            select(CompanyRecord)
            .where(CompanyRecord.tenant_id == tenant_id)
            .order_by(CompanyRecord.created_at)
        )
        return [self._company(record) for record in records]

    def save_company(self, value: Company) -> None:
        record = self._required(CompanyRecord, value.id)
        record.status = value.status.value
        record.version = value.version
        record.updated_at = value.updated_at

    def add_unit(self, value: OrganizationUnit) -> None:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        self._session.add(OrganizationUnitRecord(**data))

    def get_unit(self, unit_id: UUID) -> OrganizationUnit | None:
        record = self._session.get(OrganizationUnitRecord, unit_id)
        return self._unit(record) if record else None

    def get_unit_by_key(self, company_id: UUID, key: str) -> OrganizationUnit | None:
        record = self._session.scalar(
            select(OrganizationUnitRecord).where(
                OrganizationUnitRecord.company_id == company_id,
                OrganizationUnitRecord.key == key,
            )
        )
        return self._unit(record) if record else None

    def list_units(self, company_id: UUID) -> list[OrganizationUnit]:
        records = self._session.scalars(
            select(OrganizationUnitRecord)
            .where(OrganizationUnitRecord.company_id == company_id)
            .order_by(OrganizationUnitRecord.created_at)
        )
        return [self._unit(record) for record in records]

    def add_position(self, value: Position) -> None:
        data = dict(value.__dict__)
        data["required_capabilities"] = list(value.required_capabilities)
        data["allowed_tool_capabilities"] = list(value.allowed_tool_capabilities)
        data["status"] = value.status.value
        self._session.add(CompanyPositionRecord(**data))

    def get_position(self, position_id: UUID) -> Position | None:
        record = self._session.get(CompanyPositionRecord, position_id)
        return self._position(record) if record else None

    def get_position_by_key(self, company_id: UUID, key: str) -> Position | None:
        record = self._session.scalar(
            select(CompanyPositionRecord).where(
                CompanyPositionRecord.company_id == company_id,
                CompanyPositionRecord.key == key,
            )
        )
        return self._position(record) if record else None

    def list_positions(self, company_id: UUID) -> list[Position]:
        records = self._session.scalars(
            select(CompanyPositionRecord)
            .where(CompanyPositionRecord.company_id == company_id)
            .order_by(CompanyPositionRecord.created_at)
        )
        return [self._position(record) for record in records]

    def add_appointment(self, value: Appointment) -> None:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        self._session.add(AppointmentRecord(**data))

    def get_appointment(
        self, appointment_id: UUID, *, for_update: bool = False
    ) -> Appointment | None:
        record = self._session.get(
            AppointmentRecord, appointment_id, with_for_update=for_update
        )
        return self._appointment(record) if record else None

    def get_active_appointment(self, position_id: UUID) -> Appointment | None:
        record = self._session.scalar(
            select(AppointmentRecord).where(
                AppointmentRecord.position_id == position_id,
                AppointmentRecord.status == AppointmentStatus.ACTIVE.value,
            )
        )
        return self._appointment(record) if record else None

    def list_appointments(self, company_id: UUID) -> list[Appointment]:
        records = self._session.scalars(
            select(AppointmentRecord)
            .where(AppointmentRecord.company_id == company_id)
            .order_by(AppointmentRecord.created_at)
        )
        return [self._appointment(record) for record in records]

    def save_appointment(self, value: Appointment) -> None:
        record = self._required(AppointmentRecord, value.id)
        record.status = value.status.value
        record.ends_at = value.ends_at
        record.updated_at = value.updated_at

    def add_relationship(self, value: OrganizationRelationship) -> None:
        data = dict(value.__dict__)
        data["source_type"] = value.source_type.value
        data["target_type"] = value.target_type.value
        data["status"] = value.status.value
        self._session.add(
            OrganizationRelationshipRecord(**data)
        )

    def find_active_relationship(
        self,
        *,
        company_id: UUID,
        relationship_type: str,
        source_id: UUID,
        target_id: UUID,
    ) -> OrganizationRelationship | None:
        record = self._session.scalar(
            select(OrganizationRelationshipRecord).where(
                OrganizationRelationshipRecord.company_id == company_id,
                OrganizationRelationshipRecord.relationship_type == relationship_type,
                OrganizationRelationshipRecord.source_id == source_id,
                OrganizationRelationshipRecord.target_id == target_id,
                OrganizationRelationshipRecord.status == ResourceStatus.ACTIVE.value,
            )
        )
        return self._relationship(record) if record else None

    def list_relationships(self, company_id: UUID) -> list[OrganizationRelationship]:
        records = self._session.scalars(
            select(OrganizationRelationshipRecord)
            .where(OrganizationRelationshipRecord.company_id == company_id)
            .order_by(OrganizationRelationshipRecord.created_at)
        )
        return [self._relationship(record) for record in records]

    def _required(self, model, record_id: UUID):
        record = self._session.get(model, record_id)
        if record is None:
            raise LookupError(record_id)
        return record

    @staticmethod
    def _company(record: CompanyRecord) -> Company:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        data["status"] = CompanyStatus(record.status)
        return Company(**data)

    @staticmethod
    def _unit(record: OrganizationUnitRecord) -> OrganizationUnit:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        data["status"] = ResourceStatus(record.status)
        return OrganizationUnit(**data)

    @staticmethod
    def _position(record: CompanyPositionRecord) -> Position:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        data["required_capabilities"] = tuple(record.required_capabilities)
        data["allowed_tool_capabilities"] = tuple(record.allowed_tool_capabilities)
        data["status"] = ResourceStatus(record.status)
        return Position(**data)

    @staticmethod
    def _appointment(record: AppointmentRecord) -> Appointment:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        data["status"] = AppointmentStatus(record.status)
        return Appointment(**data)

    @staticmethod
    def _relationship(record: OrganizationRelationshipRecord) -> OrganizationRelationship:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        data["source_type"] = OrganizationNodeType(record.source_type)
        data["target_type"] = OrganizationNodeType(record.target_type)
        data["status"] = ResourceStatus(record.status)
        return OrganizationRelationship(**data)
