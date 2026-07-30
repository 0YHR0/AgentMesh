from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.api.schemas import TaskResponse
from agentmesh.application.company_operation_services import (
    OperationLaunch,
    OperationSnapshot,
)
from agentmesh.domain.company_operations import (
    MissedSchedulePolicy,
    OccurrenceStatus,
    OperationStatus,
    TriggerKind,
)


class CreateOperationRequest(BaseModel):
    organization_unit_id: UUID
    initiative_id: UUID | None = None
    key: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=160)
    objective_template: str = Field(min_length=1, max_length=20_000)
    input_template: dict[str, Any] = Field(default_factory=dict)
    trigger_kind: TriggerKind
    trigger_definition: dict[str, Any] = Field(default_factory=dict)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    missed_policy: MissedSchedulePolicy = MissedSchedulePolicy.LATEST
    catch_up_limit: int = Field(default=1, ge=1, le=100)
    concurrency_limit: int = Field(default=1, ge=1, le=100)
    maximum_runs_per_window: int = Field(default=100, ge=1)
    window_seconds: int = Field(default=86_400, ge=1)
    position_bindings: list[UUID] = Field(default_factory=list)
    tool_capability_allowlist: list[str] = Field(default_factory=list)
    budget_limit: dict[str, Any] = Field(default_factory=dict)
    approval_policy_id: UUID | None = None


class OperationTransitionRequest(BaseModel):
    action: str = Field(min_length=3, max_length=32)


class TriggerOperationRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    scheduled_at: datetime | None = None


class DispatchOperationsRequest(BaseModel):
    now: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ActivateStaffedOperationsRequest(BaseModel):
    operation_keys: list[str] = Field(default_factory=list, max_length=50)
    activated_at: datetime | None = None


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    organization_unit_id: UUID
    initiative_id: UUID | None
    key: str
    name: str
    objective_template: str
    input_template: dict[str, Any]
    trigger_kind: TriggerKind
    trigger_definition: dict[str, Any]
    timezone: str
    missed_policy: MissedSchedulePolicy
    catch_up_limit: int
    concurrency_limit: int
    maximum_runs_per_window: int
    window_seconds: int
    position_bindings: list[UUID]
    tool_capability_allowlist: list[str]
    budget_limit: dict[str, Any]
    approval_policy_id: UUID | None
    status: OperationStatus
    version: int
    content_digest: str
    created_at: datetime
    updated_at: datetime


class TriggerStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    trigger_version: int
    next_due_at: datetime | None
    last_evaluated_at: datetime | None
    last_fired_at: datetime | None
    consecutive_failures: int
    paused_reason: str | None
    fencing_token: int
    updated_at: datetime


class OccurrenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    operation_id: UUID
    operation_version: int
    occurrence_key: str
    scheduled_at: datetime
    status: OccurrenceStatus
    task_id: UUID | None
    detail: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class OperationExceptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    operation_id: UUID
    occurrence_id: UUID | None
    code: str
    message: str
    retryable: bool
    next_retry_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class OperationSnapshotResponse(BaseModel):
    operation: OperationResponse
    trigger_state: TriggerStateResponse | None
    occurrences: list[OccurrenceResponse]
    exceptions: list[OperationExceptionResponse]

    @classmethod
    def from_snapshot(cls, value: OperationSnapshot) -> "OperationSnapshotResponse":
        return cls(
            operation=OperationResponse.model_validate(value.operation),
            trigger_state=(
                TriggerStateResponse.model_validate(value.trigger_state)
                if value.trigger_state
                else None
            ),
            occurrences=[
                OccurrenceResponse.model_validate(item) for item in value.occurrences
            ],
            exceptions=[
                OperationExceptionResponse.model_validate(item)
                for item in value.exceptions
            ],
        )


class OperationLaunchResponse(BaseModel):
    occurrence: OccurrenceResponse
    task: TaskResponse | None

    @classmethod
    def from_launch(cls, value: OperationLaunch) -> "OperationLaunchResponse":
        return cls(
            occurrence=OccurrenceResponse.model_validate(value.occurrence),
            task=TaskResponse.from_aggregate(value.task) if value.task else None,
        )
