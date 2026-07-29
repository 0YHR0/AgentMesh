from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.api.schemas import TaskResponse
from agentmesh.application.company_goal_services import CycleSnapshot, InitiativeTaskLaunch
from agentmesh.domain.company_goals import (
    InitiativeStatus,
    KeyResultStatus,
    ObjectiveStatus,
    OperatingCycleStatus,
)


class CreateCycleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    starts_at: datetime
    ends_at: datetime
    review_schedule: dict[str, Any] = Field(default_factory=dict)


class TransitionRequest(BaseModel):
    action: str = Field(min_length=3, max_length=32)


class CreateObjectiveRequest(BaseModel):
    owner_position_id: UUID
    statement: str = Field(min_length=1, max_length=2_000)
    rationale: str = Field(min_length=1, max_length=10_000)
    priority: int = Field(ge=1, le=5)
    target_date: datetime


class CreateKeyResultRequest(BaseModel):
    metric_key: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=32)
    baseline: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=80)
    measurement_source: str = Field(min_length=1, max_length=255)


class RecordKeyResultRequest(BaseModel):
    value: str = Field(min_length=1, max_length=80)
    verified: bool = False
    source: str | None = Field(default=None, max_length=255)


class CreateInitiativeRequest(BaseModel):
    owner_unit_id: UUID
    title: str = Field(min_length=1, max_length=240)
    outcome_contract: dict[str, Any]
    budget_allocation_id: UUID | None = None


class LaunchInitiativeTaskRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)


class CycleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    name: str
    starts_at: datetime
    ends_at: datetime
    status: OperatingCycleStatus
    approved_by: str | None
    approved_at: datetime | None
    review_schedule: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class ObjectiveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    cycle_id: UUID
    owner_position_id: UUID
    statement: str
    rationale: str
    status: ObjectiveStatus
    priority: int
    target_date: datetime
    version: int
    created_at: datetime
    updated_at: datetime


class KeyResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    objective_id: UUID
    metric_key: str
    unit: str
    baseline: str
    target: str
    current_verified_value: str | None
    current_estimated_value: str | None
    measurement_source: str
    status: KeyResultStatus
    version: int
    created_at: datetime
    updated_at: datetime


class InitiativeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    objective_id: UUID
    owner_unit_id: UUID
    title: str
    outcome_contract: dict[str, Any]
    budget_allocation_id: UUID | None
    status: InitiativeStatus
    starts_at: datetime
    ends_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class InitiativeTaskLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    initiative_id: UUID
    task_id: UUID
    created_by: str
    created_at: datetime


class ObjectiveSnapshotResponse(BaseModel):
    objective: ObjectiveResponse
    key_results: list[KeyResultResponse]
    initiatives: list[InitiativeResponse]
    task_links: dict[UUID, list[InitiativeTaskLinkResponse]]


class CycleSnapshotResponse(BaseModel):
    cycle: CycleResponse
    objectives: list[ObjectiveSnapshotResponse]

    @classmethod
    def from_snapshot(cls, value: CycleSnapshot) -> "CycleSnapshotResponse":
        return cls(
            cycle=CycleResponse.model_validate(value.cycle),
            objectives=[
                ObjectiveSnapshotResponse(
                    objective=ObjectiveResponse.model_validate(item.objective),
                    key_results=[
                        KeyResultResponse.model_validate(result)
                        for result in item.key_results
                    ],
                    initiatives=[
                        InitiativeResponse.model_validate(initiative)
                        for initiative in item.initiatives
                    ],
                    task_links={
                        key: [
                            InitiativeTaskLinkResponse.model_validate(link)
                            for link in links
                        ]
                        for key, links in item.task_links.items()
                    },
                )
                for item in value.objectives
            ],
        )


class InitiativeTaskLaunchResponse(BaseModel):
    task: TaskResponse
    link: InitiativeTaskLinkResponse

    @classmethod
    def from_launch(
        cls, value: InitiativeTaskLaunch
    ) -> "InitiativeTaskLaunchResponse":
        return cls(
            task=TaskResponse.from_aggregate(value.task),
            link=InitiativeTaskLinkResponse.model_validate(value.link),
        )
