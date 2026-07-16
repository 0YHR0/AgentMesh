from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from agentmesh.api.schemas import CreateTaskRequest, TaskListResponse, TaskResponse
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.tasks import TaskStatus

router = APIRouter()


def get_task_service(request: Request) -> TaskApplicationService:
    return request.app.state.container.task_service


TaskServiceDependency = Annotated[TaskApplicationService, Depends(get_task_service)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]
StatusQuery = Annotated[TaskStatus | None, Query(alias="status")]


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
) -> TaskResponse:
    aggregate = service.create_task(objective=payload.objective, input=payload.input)
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


@router.post(
    "/api/v1/tasks/{task_id}/runs",
    response_model=TaskResponse,
    tags=["tasks"],
)
def run_task(
    task_id: UUID,
    service: TaskServiceDependency,
) -> TaskResponse:
    return TaskResponse.from_aggregate(service.run_task(task_id))


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
