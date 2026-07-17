from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from agentmesh.api.feature_routes import FeatureGatesDependency
from agentmesh.api.schemas import (
    CreateTaskRequest,
    TaskListResponse,
    TaskResponse,
    TaskUsageResponse,
)
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import Feature

router = APIRouter()


def get_task_service(request: Request) -> TaskApplicationService:
    return request.app.state.container.task_service


TaskServiceDependency = Annotated[TaskApplicationService, Depends(get_task_service)]


def get_usage_service(request: Request) -> UsageQueryService:
    return request.app.state.container.usage_service


UsageServiceDependency = Annotated[UsageQueryService, Depends(get_usage_service)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]
StatusQuery = Annotated[TaskStatus | None, Query(alias="status")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", tags=["system"])
def ready(request: Request, response: Response) -> dict[str, str]:
    if request.app.state.container.readiness_probe.is_ready():
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready"}


@router.post(
    "/api/v1/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
)
def create_task(
    payload: CreateTaskRequest,
    service: TaskServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> TaskResponse:
    if "tool_call" in payload.input:
        feature_gates.require(Feature.MCP_READ_TOOLS)
    if payload.execution_mode.value == "REVIEWED":
        feature_gates.require(Feature.REVIEWED_EXECUTION)
    if payload.execution_mode.value == "COORDINATED":
        feature_gates.require(Feature.COORDINATED_EXECUTION)
    coordinated_plan = (
        CoordinatedPlan.create(
            tuple(subtask.to_domain() for subtask in payload.subtasks),
            max_concurrency=payload.max_concurrency,
        )
        if payload.subtasks
        else None
    )
    aggregate = service.create_task(
        objective=payload.objective,
        input=payload.input,
        execution_mode=payload.execution_mode,
        acceptance_criteria=tuple(
            criterion.to_domain() for criterion in payload.acceptance_criteria
        ),
        max_revisions=payload.max_revisions,
        review_deadline=payload.review_deadline,
        coordinated_plan=coordinated_plan,
    )
    return TaskResponse.from_aggregate(aggregate)


@router.get("/api/v1/tasks", response_model=TaskListResponse, tags=["tasks"])
def list_tasks(
    service: TaskServiceDependency,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
    task_status: StatusQuery = None,
) -> TaskListResponse:
    aggregates = service.list_tasks(limit=limit, offset=offset, status=task_status)
    return TaskListResponse(
        items=[TaskResponse.from_aggregate(aggregate) for aggregate in aggregates],
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/tasks/{task_id}", response_model=TaskResponse, tags=["tasks"])
def get_task(
    task_id: UUID,
    service: TaskServiceDependency,
) -> TaskResponse:
    return TaskResponse.from_aggregate(service.get_task(task_id))


@router.get(
    "/api/v1/tasks/{task_id}/usage",
    response_model=TaskUsageResponse,
    tags=["observability"],
)
def get_task_usage(
    task_id: UUID,
    service: UsageServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> TaskUsageResponse:
    feature_gates.require(Feature.OBSERVABILITY)
    return TaskUsageResponse.from_task_usage(service.get_task_usage(task_id))


@router.post(
    "/api/v1/tasks/{task_id}/runs",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
)
def run_task(
    task_id: UUID,
    service: TaskServiceDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> TaskResponse:
    aggregate = service.request_run(task_id, idempotency_key=idempotency_key)
    response.headers["Location"] = f"/api/v1/tasks/{aggregate.task.id}"
    return TaskResponse.from_aggregate(aggregate)


@router.post(
    "/api/v1/tasks/{task_id}/pause",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
)
def pause_task(
    task_id: UUID,
    service: TaskServiceDependency,
    response: Response,
) -> TaskResponse:
    aggregate = service.pause_task(task_id)
    response.headers["Location"] = f"/api/v1/tasks/{aggregate.task.id}"
    return TaskResponse.from_aggregate(aggregate)


@router.post(
    "/api/v1/tasks/{task_id}/resume",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
)
def resume_task(
    task_id: UUID,
    service: TaskServiceDependency,
    response: Response,
) -> TaskResponse:
    aggregate = service.resume_task(task_id)
    response.headers["Location"] = f"/api/v1/tasks/{aggregate.task.id}"
    return TaskResponse.from_aggregate(aggregate)


@router.post(
    "/api/v1/tasks/{task_id}/cancel",
    response_model=TaskResponse,
    tags=["tasks"],
)
def cancel_task(
    task_id: UUID,
    service: TaskServiceDependency,
) -> TaskResponse:
    return TaskResponse.from_aggregate(service.cancel_task(task_id))
