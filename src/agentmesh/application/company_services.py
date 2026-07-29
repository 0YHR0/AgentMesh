from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.company import (
    Appointment,
    Company,
    CompanyStatus,
    OrganizationNodeType,
    OrganizationRelationship,
    OrganizationUnit,
    Position,
)
from agentmesh.domain.errors import (
    CompanyModelConflict,
    CompanyModelNotFound,
    InvalidCompanyModel,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.registry import AgentVersionStatus
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class CompanySnapshot:
    company: Company
    units: list[OrganizationUnit]
    positions: list[Position]
    appointments: list[Appointment]
    relationships: list[OrganizationRelationship]


class CompanyModelService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates

    def create_company(
        self,
        *,
        name: str,
        mission: str,
        owner_principal_id: str,
        risk_policy_id: UUID | None = None,
        default_currency: str = "USD",
        operating_timezone: str = "UTC",
    ) -> Company:
        self._require_enabled()
        company = Company.create(
            tenant_id=self._tenant_id,
            name=name,
            mission=mission,
            owner_principal_id=owner_principal_id,
            risk_policy_id=risk_policy_id,
            default_currency=default_currency,
            operating_timezone=operating_timezone,
        )
        with self._uow_factory() as uow:
            uow.idempotency.lock(f"active-company:{self._tenant_id}", self._tenant_id)
            if uow.company_model.get_active_company(self._tenant_id) is not None:
                raise CompanyModelConflict("This tenant already has an active Company")
            uow.company_model.add_company(company)
            uow.outbox.add(self._event("company.created", company.id, {"name": company.name}))
            uow.commit()
        return company

    def get_company(self, company_id: UUID) -> CompanySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            return self._snapshot(uow, company)

    def get_active_company(self) -> CompanySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                raise CompanyModelNotFound("No active Company exists")
            return self._snapshot(uow, company)

    def list_companies(self) -> list[Company]:
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.company_model.list_companies(self._tenant_id)

    def archive_company(self, company_id: UUID) -> Company:
        self._require_enabled()
        with self._uow_factory() as uow:
            company = self._company(uow, company_id, for_update=True)
            if any(
                value.status.value == "ACTIVE"
                for value in uow.company_model.list_appointments(company.id)
            ):
                raise CompanyModelConflict("End active Appointments before archiving the Company")
            company.archive()
            uow.company_model.save_company(company)
            uow.outbox.add(self._event("company.archived", company.id, {}))
            uow.commit()
            return company

    def create_unit(self, company_id: UUID, **values: Any) -> OrganizationUnit:
        self._require_enabled()
        unit = OrganizationUnit.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            self._require_active(company)
            if uow.company_model.get_unit_by_key(company.id, unit.key) is not None:
                raise CompanyModelConflict(f"Organization Unit key '{unit.key}' already exists")
            if unit.parent_unit_id is not None:
                parent = self._unit(uow, unit.parent_unit_id)
                self._require_same_company(parent.company_id, company.id)
            uow.company_model.add_unit(unit)
            uow.outbox.add(
                self._event(
                    "organization-unit.created",
                    unit.id,
                    {"company_id": str(company.id), "key": unit.key, "kind": unit.kind},
                )
            )
            uow.commit()
        return unit

    def create_position(self, company_id: UUID, **values: Any) -> Position:
        self._require_enabled()
        position = Position.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            self._require_active(company)
            unit = self._unit(uow, position.primary_unit_id)
            self._require_same_company(unit.company_id, company.id)
            if uow.company_model.get_position_by_key(company.id, position.key) is not None:
                raise CompanyModelConflict(f"Position key '{position.key}' already exists")
            if position.reports_to_position_id is not None:
                manager = self._position(uow, position.reports_to_position_id)
                self._require_same_company(manager.company_id, company.id)
            uow.company_model.add_position(position)
            uow.outbox.add(
                self._event(
                    "position.created",
                    position.id,
                    {
                        "company_id": str(company.id),
                        "unit_id": str(unit.id),
                        "key": position.key,
                    },
                )
            )
            uow.commit()
        return position

    def appoint(
        self,
        company_id: UUID,
        *,
        position_id: UUID,
        agent_definition_id: UUID,
        agent_version_id: UUID,
        appointed_by: str,
        reason: str,
    ) -> Appointment:
        self._require_enabled()
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            self._require_active(company)
            position = self._position(uow, position_id)
            self._require_same_company(position.company_id, company.id)
            if uow.company_model.get_active_appointment(position.id) is not None:
                raise CompanyModelConflict("Position already has an active Appointment")
            definition = uow.agent_definitions.get(agent_definition_id)
            version = uow.agent_versions.get(agent_version_id)
            if (
                definition is None
                or definition.tenant_id != self._tenant_id
                or version is None
                or version.definition_id != definition.id
            ):
                raise InvalidCompanyModel(
                    "Appointment Agent Version must belong to the selected tenant Agent Definition"
                )
            if version.status is not AgentVersionStatus.PUBLISHED:
                raise InvalidCompanyModel("Only a published Agent Version may be appointed")
            missing = set(position.required_capabilities) - set(version.verified_capabilities)
            if missing:
                raise InvalidCompanyModel(
                    "Agent Version lacks required Position capabilities: "
                    + ", ".join(sorted(missing))
                )
            appointment = Appointment.create(
                company_id=company.id,
                position_id=position.id,
                agent_definition_id=definition.id,
                agent_version_id=version.id,
                appointed_by=appointed_by,
                reason=reason,
            )
            uow.company_model.add_appointment(appointment)
            uow.outbox.add(
                self._event(
                    "appointment.started",
                    appointment.id,
                    {
                        "company_id": str(company.id),
                        "position_id": str(position.id),
                        "agent_definition_id": str(definition.id),
                        "agent_version_id": str(version.id),
                    },
                )
            )
            uow.commit()
            return appointment

    def end_appointment(self, company_id: UUID, appointment_id: UUID) -> Appointment:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            appointment = uow.company_model.get_appointment(appointment_id, for_update=True)
            if appointment is None or appointment.company_id != company_id:
                raise CompanyModelNotFound(f"Appointment {appointment_id} was not found")
            appointment.end()
            uow.company_model.save_appointment(appointment)
            uow.outbox.add(
                self._event(
                    "appointment.ended",
                    appointment.id,
                    {"company_id": str(company_id), "position_id": str(appointment.position_id)},
                )
            )
            uow.commit()
            return appointment

    def create_relationship(
        self,
        company_id: UUID,
        *,
        relationship_type: str,
        source_type: OrganizationNodeType,
        source_id: UUID,
        target_type: OrganizationNodeType,
        target_id: UUID,
        attributes: dict[str, Any] | None = None,
    ) -> OrganizationRelationship:
        self._require_enabled()
        relationship = OrganizationRelationship.create(
            company_id=company_id,
            relationship_type=relationship_type,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            attributes=attributes,
        )
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            self._require_active(company)
            self._node(uow, company.id, source_type, source_id)
            self._node(uow, company.id, target_type, target_id)
            existing = uow.company_model.find_active_relationship(
                company_id=company.id,
                relationship_type=relationship.relationship_type,
                source_id=source_id,
                target_id=target_id,
            )
            if existing is not None:
                raise CompanyModelConflict("Organization relationship already exists")
            uow.company_model.add_relationship(relationship)
            uow.outbox.add(
                self._event(
                    "organization-relationship.created",
                    relationship.id,
                    {
                        "company_id": str(company.id),
                        "relationship_type": relationship.relationship_type,
                        "source_id": str(source_id),
                        "target_id": str(target_id),
                    },
                )
            )
            uow.commit()
            return relationship

    def _snapshot(self, uow: Any, company: Company) -> CompanySnapshot:
        return CompanySnapshot(
            company=company,
            units=uow.company_model.list_units(company.id),
            positions=uow.company_model.list_positions(company.id),
            appointments=uow.company_model.list_appointments(company.id),
            relationships=uow.company_model.list_relationships(company.id),
        )

    def _company(self, uow: Any, company_id: UUID, *, for_update: bool = False) -> Company:
        company = uow.company_model.get_company(company_id, for_update=for_update)
        if company is None or company.tenant_id != self._tenant_id:
            raise CompanyModelNotFound(f"Company {company_id} was not found")
        return company

    @staticmethod
    def _unit(uow: Any, unit_id: UUID) -> OrganizationUnit:
        unit = uow.company_model.get_unit(unit_id)
        if unit is None:
            raise CompanyModelNotFound(f"Organization Unit {unit_id} was not found")
        return unit

    @staticmethod
    def _position(uow: Any, position_id: UUID) -> Position:
        position = uow.company_model.get_position(position_id)
        if position is None:
            raise CompanyModelNotFound(f"Position {position_id} was not found")
        return position

    def _node(
        self, uow: Any, company_id: UUID, node_type: OrganizationNodeType, node_id: UUID
    ) -> None:
        value = (
            self._unit(uow, node_id)
            if node_type is OrganizationNodeType.UNIT
            else self._position(uow, node_id)
        )
        self._require_same_company(value.company_id, company_id)

    @staticmethod
    def _require_same_company(actual: UUID, expected: UUID) -> None:
        if actual != expected:
            raise InvalidCompanyModel("Referenced organization resource belongs to another Company")

    @staticmethod
    def _require_active(company: Company) -> None:
        if company.status is not CompanyStatus.ACTIVE:
            raise CompanyModelConflict("Archived Company cannot be modified")

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.COMPANY_MODEL)

    def _event(self, suffix: str, aggregate_id: UUID, payload: dict[str, Any]) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name=f"agentmesh.{suffix}",
            tenant_id=self._tenant_id,
            aggregate_id=aggregate_id,
            payload=payload,
        )
