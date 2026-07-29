from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.application.business_object_services import BusinessObjectSnapshot
from agentmesh.domain.business_objects import (
    BusinessObjectTypeStatus,
    ObjectSourceType,
)


class CreateBusinessObjectTypeRequest(BaseModel):
    key: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=160)
    schema_version: int = Field(ge=1)
    json_schema: dict[str, Any]
    lifecycle_definition: dict[str, Any]
    sensitive_fields: list[str] = Field(default_factory=list)
    ownership_rules: dict[str, Any] = Field(default_factory=dict)
    retention_policy: dict[str, Any] = Field(default_factory=dict)


class BusinessObjectTypeTransitionRequest(BaseModel):
    action: str = Field(min_length=3, max_length=32)


class CreateBusinessObjectRequest(BaseModel):
    type_id: UUID
    data: dict[str, Any]
    source_type: ObjectSourceType = ObjectSourceType.USER
    source_id: str | None = Field(default=None, max_length=255)
    external_ref: str | None = Field(default=None, max_length=255)
    owner_position_id: UUID | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class ApplyBusinessObjectActionRequest(BaseModel):
    action_key: str = Field(min_length=1, max_length=63)
    expected_revision: int = Field(ge=1)
    input: dict[str, Any] = Field(default_factory=dict)
    source_type: ObjectSourceType = ObjectSourceType.USER
    source_id: str | None = Field(default=None, max_length=255)
    evidence_refs: list[str] = Field(default_factory=list)
    actor_position_key: str | None = Field(default=None, max_length=63)
    actor_capabilities: list[str] = Field(default_factory=list)


class BusinessObjectTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    key: str
    name: str
    schema_version: int
    json_schema: dict[str, Any]
    lifecycle_definition: dict[str, Any]
    sensitive_fields: list[str]
    ownership_rules: dict[str, Any]
    retention_policy: dict[str, Any]
    status: BusinessObjectTypeStatus
    content_digest: str
    created_at: datetime
    updated_at: datetime


class BusinessObjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    type_id: UUID
    external_ref: str | None
    current_revision: int
    lifecycle_state: str
    owner_position_id: UUID | None
    created_at: datetime
    updated_at: datetime


class BusinessObjectRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    object_id: UUID
    revision: int
    schema_version: int
    action: str
    data: dict[str, Any]
    data_digest: str
    source_type: ObjectSourceType
    source_id: str | None
    actor: str
    evidence_refs: list[str]
    created_at: datetime


class BusinessObjectSnapshotResponse(BaseModel):
    object: BusinessObjectResponse
    type: BusinessObjectTypeResponse
    revisions: list[BusinessObjectRevisionResponse]

    @classmethod
    def from_snapshot(
        cls, value: BusinessObjectSnapshot
    ) -> "BusinessObjectSnapshotResponse":
        return cls(
            object=BusinessObjectResponse.model_validate(value.object),
            type=BusinessObjectTypeResponse.model_validate(value.type),
            revisions=[
                BusinessObjectRevisionResponse.model_validate(revision)
                for revision in value.revisions
            ],
        )
