from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.business_objects import (
    BusinessObject,
    BusinessObjectRevision,
    BusinessObjectType,
    BusinessObjectTypeStatus,
    ObjectSourceType,
    validate_data,
)
from agentmesh.domain.company import CompanyStatus, ResourceStatus
from agentmesh.domain.errors import (
    BusinessObjectConflict,
    BusinessObjectNotFound,
    InvalidBusinessObject,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class BusinessObjectSnapshot:
    object: BusinessObject
    type: BusinessObjectType
    revisions: list[BusinessObjectRevision]


class BusinessObjectService:
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

    def create_type(self, company_id: UUID, **values: Any) -> BusinessObjectType:
        self._require_enabled()
        object_type = BusinessObjectType.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            if uow.business_objects.get_type_by_key(
                company_id,
                object_type.key,
                schema_version=object_type.schema_version,
            ):
                raise BusinessObjectConflict(
                    "Business Object Type key and schema version already exist"
                )
            uow.business_objects.add_type(object_type)
            self._emit(
                uow,
                "object-type.created",
                company_id=company_id,
                aggregate_id=object_type.id,
                payload={
                    "type_key": object_type.key,
                    "schema_version": object_type.schema_version,
                    "content_digest": object_type.content_digest,
                },
            )
            uow.commit()
        return object_type

    def transition_type(
        self, company_id: UUID, type_id: UUID, action: str
    ) -> BusinessObjectType:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            object_type = self._type(uow, company_id, type_id, for_update=True)
            if action == "publish":
                current = uow.business_objects.get_type_by_key(
                    company_id, object_type.key, published_only=True
                )
                if current is not None and current.id != object_type.id:
                    if current.schema_version >= object_type.schema_version:
                        raise BusinessObjectConflict(
                            "Published Business Object Type version must increase"
                        )
                    current.deprecate()
                    uow.business_objects.save_type(current)
                object_type.publish()
            elif action == "deprecate":
                object_type.deprecate()
            else:
                raise InvalidBusinessObject(
                    f"Unknown Business Object Type action '{action}'"
                )
            uow.business_objects.save_type(object_type)
            self._emit(
                uow,
                f"object-type.{action}d",
                company_id=company_id,
                aggregate_id=object_type.id,
                payload={
                    "type_key": object_type.key,
                    "schema_version": object_type.schema_version,
                    "content_digest": object_type.content_digest,
                },
            )
            uow.commit()
            return object_type

    def list_types(self, company_id: UUID) -> list[BusinessObjectType]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.business_objects.list_types(company_id)

    def create_object(
        self,
        company_id: UUID,
        *,
        type_id: UUID,
        data: dict[str, Any],
        actor: str,
        source_type: ObjectSourceType = ObjectSourceType.USER,
        source_id: str | None = None,
        external_ref: str | None = None,
        owner_position_id: UUID | None = None,
        evidence_refs: list[str] | None = None,
    ) -> BusinessObjectSnapshot:
        self._require_enabled()
        normalized_actor = actor.strip()
        if not normalized_actor:
            raise InvalidBusinessObject("Business Object actor is required")
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            object_type = self._type(uow, company_id, type_id)
            if object_type.status is not BusinessObjectTypeStatus.PUBLISHED:
                raise BusinessObjectConflict(
                    "Only a published Business Object Type can create Objects"
                )
            validate_data(object_type.json_schema, data, label="Business Object data")
            if (
                object_type.ownership_rules.get("position_required")
                and owner_position_id is None
            ):
                raise InvalidBusinessObject(
                    "Business Object Type requires an owner Position"
                )
            self._validate_owner(uow, company_id, owner_position_id)
            if external_ref and uow.business_objects.get_object_by_external_ref(
                type_id, external_ref.strip()
            ):
                raise BusinessObjectConflict(
                    "External reference already exists for this Object Type"
                )
            value = BusinessObject.create(
                company_id=company_id,
                type_id=type_id,
                initial_state=object_type.initial_state,
                external_ref=external_ref,
                owner_position_id=owner_position_id,
            )
            revision = BusinessObjectRevision.create(
                object_id=value.id,
                revision=1,
                schema_version=object_type.schema_version,
                action="create",
                data=data,
                source_type=source_type,
                source_id=source_id,
                actor=normalized_actor,
                evidence_refs=evidence_refs,
            )
            uow.business_objects.add_object(value)
            uow.business_objects.add_revision(revision)
            self._emit(
                uow,
                "object.created",
                company_id=company_id,
                aggregate_id=value.id,
                payload=self._event_payload(value, object_type, revision),
            )
            uow.commit()
            return self._snapshot(value, object_type, [revision])

    def apply_action(
        self,
        company_id: UUID,
        object_id: UUID,
        *,
        action_key: str,
        expected_revision: int,
        input: dict[str, Any],
        actor: str,
        source_type: ObjectSourceType = ObjectSourceType.USER,
        source_id: str | None = None,
        evidence_refs: list[str] | None = None,
        actor_position_key: str | None = None,
        actor_capabilities: list[str] | None = None,
    ) -> BusinessObjectSnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            value = self._object(uow, company_id, object_id, for_update=True)
            object_type = self._type(uow, company_id, value.type_id)
            if expected_revision != value.current_revision:
                raise InvalidBusinessObject(
                    f"Stale Business Object revision: expected {expected_revision}, "
                    f"current is {value.current_revision}"
                )
            action = object_type.action(action_key)
            if action["side_effect_class"] != "NONE":
                raise BusinessObjectConflict(
                    f"Action '{action_key}' declares a side effect and requires a "
                    "governed external-action adapter"
                )
            if value.lifecycle_state not in action["from"]:
                raise BusinessObjectConflict(
                    f"Action '{action_key}' is not allowed from {value.lifecycle_state}"
                )
            validate_data(
                action["input_schema"],
                input,
                label=f"Business Object action '{action_key}' input",
            )
            input_fields = set(input)
            allowed_fields = set(action["allowed_update_fields"])
            if not input_fields <= allowed_fields:
                raise InvalidBusinessObject(
                    "Action attempted fields outside allowed_update_fields: "
                    + ", ".join(sorted(input_fields - allowed_fields))
                )
            evidence = sorted(set(evidence_refs or []))
            if action["required_evidence"] and not evidence:
                raise BusinessObjectConflict(
                    f"Action '{action_key}' requires evidence references"
                )
            required_positions = set(action["required_position_keys"])
            if required_positions and actor_position_key not in required_positions:
                raise BusinessObjectConflict(
                    f"Action '{action_key}' requires an allowed Position"
                )
            required_capabilities = set(action["required_capabilities"])
            if not required_capabilities <= set(actor_capabilities or []):
                raise BusinessObjectConflict(
                    f"Action '{action_key}' requires declared capabilities"
                )
            previous = uow.business_objects.get_revision(
                value.id, value.current_revision
            )
            if previous is None:
                raise BusinessObjectNotFound("Current Business Object revision was not found")
            next_data = {**previous.data, **dict(input)}
            validate_data(
                object_type.json_schema,
                next_data,
                label="Resulting Business Object data",
            )
            value.apply(
                expected_revision=expected_revision, target_state=action["to"]
            )
            revision = BusinessObjectRevision.create(
                object_id=value.id,
                revision=value.current_revision,
                schema_version=object_type.schema_version,
                action=action_key,
                data=next_data,
                source_type=source_type,
                source_id=source_id,
                actor=actor,
                evidence_refs=evidence,
            )
            uow.business_objects.save_object(value)
            uow.business_objects.add_revision(revision)
            self._emit(
                uow,
                "object.transitioned",
                company_id=company_id,
                aggregate_id=value.id,
                payload={
                    **self._event_payload(value, object_type, revision),
                    "action": action_key,
                    "side_effect_class": action["side_effect_class"],
                },
            )
            uow.commit()
            revisions = uow.business_objects.list_revisions(value.id)
            return self._snapshot(value, object_type, revisions)

    def get_object(
        self, company_id: UUID, object_id: UUID
    ) -> BusinessObjectSnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            value = self._object(uow, company_id, object_id)
            object_type = self._type(uow, company_id, value.type_id)
            return self._snapshot(
                value,
                object_type,
                uow.business_objects.list_revisions(value.id),
            )

    def list_objects(
        self,
        company_id: UUID,
        *,
        type_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BusinessObject]:
        self._require_enabled()
        if not 1 <= limit <= 500 or offset < 0:
            raise InvalidBusinessObject("Object list pagination is invalid")
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            if type_id is not None:
                self._type(uow, company_id, type_id)
            return uow.business_objects.list_objects(
                company_id, type_id=type_id, limit=limit, offset=offset
            )

    @staticmethod
    def _validate_owner(uow: Any, company_id: UUID, owner_position_id: UUID | None) -> None:
        if owner_position_id is None:
            return
        position = uow.company_model.get_position(owner_position_id)
        if (
            position is None
            or position.company_id != company_id
            or position.status is not ResourceStatus.ACTIVE
        ):
            raise InvalidBusinessObject(
                "Business Object owner must be an active Company Position"
            )

    @staticmethod
    def _event_payload(
        value: BusinessObject,
        object_type: BusinessObjectType,
        revision: BusinessObjectRevision,
    ) -> dict[str, Any]:
        return {
            "object_id": str(value.id),
            "type_id": str(object_type.id),
            "type_key": object_type.key,
            "schema_version": object_type.schema_version,
            "revision": revision.revision,
            "lifecycle_state": value.lifecycle_state,
            "data_digest": revision.data_digest,
            "evidence_count": len(revision.evidence_refs),
        }

    @staticmethod
    def _snapshot(
        value: BusinessObject,
        object_type: BusinessObjectType,
        revisions: list[BusinessObjectRevision],
    ) -> BusinessObjectSnapshot:
        redacted = [
            replace(
                revision,
                data={
                    key: ("***REDACTED***" if key in object_type.sensitive_fields else item)
                    for key, item in revision.data.items()
                },
            )
            for revision in revisions
        ]
        return BusinessObjectSnapshot(
            object=value, type=object_type, revisions=redacted
        )

    def _company(self, uow: Any, company_id: UUID):
        company = uow.company_model.get_company(company_id)
        if company is None or company.tenant_id != self._tenant_id:
            raise BusinessObjectNotFound(f"Company {company_id} was not found")
        return company

    def _active_company(self, uow: Any, company_id: UUID):
        company = self._company(uow, company_id)
        if company.status is not CompanyStatus.ACTIVE:
            raise BusinessObjectConflict("Archived Company cannot manage Business Objects")
        return company

    @staticmethod
    def _type(
        uow: Any, company_id: UUID, type_id: UUID, *, for_update: bool = False
    ) -> BusinessObjectType:
        value = uow.business_objects.get_type(type_id, for_update=for_update)
        if value is None or value.company_id != company_id:
            raise BusinessObjectNotFound(f"Business Object Type {type_id} was not found")
        return value

    @staticmethod
    def _object(
        uow: Any, company_id: UUID, object_id: UUID, *, for_update: bool = False
    ) -> BusinessObject:
        value = uow.business_objects.get_object(object_id, for_update=for_update)
        if value is None or value.company_id != company_id:
            raise BusinessObjectNotFound(f"Business Object {object_id} was not found")
        return value

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.BUSINESS_OBJECTS)

    def _emit(
        self,
        uow: Any,
        suffix: str,
        *,
        company_id: UUID,
        aggregate_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=f"agentmesh.company.{suffix}",
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"company_id": str(company_id), **payload},
            )
        )
