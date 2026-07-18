from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from agentmesh.api.feature_routes import FeatureGatesDependency
from agentmesh.api.schemas import (
    CreateTaskRequest,
    DecideHandoffRequest,
    HandoffResponse,
    IncreaseBudgetAndResumeRequest,
    RequestHandoffRequest,
    ResolveTaskRequest,
    TaskBudgetStatusResponse,
    TaskListResponse,
    TaskResolutionListResponse,
    TaskResolutionResponse,
    TaskResolutionResultResponse,
    TaskResponse,
    TaskUsageResponse,
)
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.budget_services import BudgetQueryService
from agentmesh.application.handoff_services import HandoffApplicationService
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan
from agentmesh.domain.identity import Permission
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import Feature

router = APIRouter()


def get_task_service(request: Request) -> TaskApplicationService:
    return request.app.state.container.task_service


TaskServiceDependency = Annotated[TaskApplicationService, Depends(get_task_service)]


def get_handoff_service(request: Request) -> HandoffApplicationService:
    return request.app.state.container.handoff_service


HandoffServiceDependency = Annotated[
    HandoffApplicationService, Depends(get_handoff_service)
]


def get_usage_service(request: Request) -> UsageQueryService:
    return request.app.state.container.usage_service


UsageServiceDependency = Annotated[UsageQueryService, Depends(get_usage_service)]


def get_budget_service(request: Request) -> BudgetQueryService:
    return request.app.state.container.budget_service


BudgetServiceDependency = Annotated[BudgetQueryService, Depends(get_budget_service)]


def get_resolution_service(request: Request) -> TaskResolutionService:
    return request.app.state.container.resolution_service


ResolutionServiceDependency = Annotated[
    TaskResolutionService, Depends(get_resolution_service)
]
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
    dependencies=[Depends(require_permission(Permission.TASK_CREATE))],
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
    if payload.budget is not None:
        feature_gates.require(Feature.BUDGET_ADMISSION)
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
        budget=payload.budget.to_domain() if payload.budget is not None else None,
    )
    return TaskResponse.from_aggregate(aggregate)


@router.get(
    "/api/v1/tasks",
    response_model=TaskListResponse,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_READ))],
)
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


@router.get(
    "/api/v1/tasks/{task_id}",
    response_model=TaskResponse,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_READ))],
)
def get_task(
    task_id: UUID,
    service: TaskServiceDependency,
) -> TaskResponse:
    return TaskResponse.from_aggregate(service.get_task(task_id))


@router.get(
    "/api/v1/tasks/{task_id}/usage",
    response_model=TaskUsageResponse,
    tags=["observability"],
    dependencies=[Depends(require_permission(Permission.OBSERVABILITY_READ))],
)
def get_task_usage(
    task_id: UUID,
    service: UsageServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> TaskUsageResponse:
    feature_gates.require(Feature.OBSERVABILITY)
    return TaskUsageResponse.from_task_usage(service.get_task_usage(task_id))


@router.get(
    "/api/v1/tasks/{task_id}/budget",
    response_model=TaskBudgetStatusResponse,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.OBSERVABILITY_READ))],
)
def get_task_budget(
    task_id: UUID,
    service: BudgetServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> TaskBudgetStatusResponse:
    feature_gates.require(Feature.BUDGET_ADMISSION)
    return TaskBudgetStatusResponse.from_domain(service.get_status(task_id))


def _resolution_response(result) -> TaskResolutionResultResponse:
    return TaskResolutionResultResponse(
        resolution=TaskResolutionResponse.from_domain(result.resolution),
        task=TaskResponse.from_aggregate(result.aggregate),
    )


@router.get(
    "/api/v1/tasks/{task_id}/resolutions",
    response_model=TaskResolutionListResponse,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_READ))],
)
def list_task_resolutions(
    task_id: UUID,
    service: ResolutionServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> TaskResolutionListResponse:
    feature_gates.require(Feature.HUMAN_RESOLUTION)
    return TaskResolutionListResponse(
        items=[
            TaskResolutionResponse.from_domain(value)
            for value in service.list_resolutions(task_id)
        ]
    )


@router.post(
    "/api/v1/tasks/{task_id}/resolutions/accept-candidate",
    response_model=TaskResolutionResultResponse,
    status_code=status.HTTP_200_OK,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_RESOLVE))],
)
def accept_task_candidate(
    task_id: UUID,
    payload: ResolveTaskRequest,
    service: ResolutionServiceDependency,
    feature_gates: FeatureGatesDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> TaskResolutionResultResponse:
    feature_gates.require(Feature.HUMAN_RESOLUTION)
    return _resolution_response(
        service.accept_candidate(
            task_id,
            actor=principal.audit_actor(payload.actor),
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/api/v1/tasks/{task_id}/resolutions/reject",
    response_model=TaskResolutionResultResponse,
    status_code=status.HTTP_200_OK,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_RESOLVE))],
)
def reject_waiting_task(
    task_id: UUID,
    payload: ResolveTaskRequest,
    service: ResolutionServiceDependency,
    feature_gates: FeatureGatesDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> TaskResolutionResultResponse:
    feature_gates.require(Feature.HUMAN_RESOLUTION)
    return _resolution_response(
        service.reject_task(
            task_id,
            actor=principal.audit_actor(payload.actor),
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/api/v1/tasks/{task_id}/resolutions/increase-budget-and-resume",
    response_model=TaskResolutionResultResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_RESOLVE))],
)
def increase_budget_and_resume(
    task_id: UUID,
    payload: IncreaseBudgetAndResumeRequest,
    service: ResolutionServiceDependency,
    feature_gates: FeatureGatesDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> TaskResolutionResultResponse:
    feature_gates.require(Feature.HUMAN_RESOLUTION)
    feature_gates.require(Feature.BUDGET_ADMISSION)
    return _resolution_response(
        service.increase_budget_and_resume(
            task_id,
            replacement=payload.budget.to_domain(),
            actor=principal.audit_actor(payload.actor),
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/api/v1/tasks/{task_id}/runs",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
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
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
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
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
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
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def cancel_task(
    task_id: UUID,
    service: TaskServiceDependency,
) -> TaskResponse:
    return TaskResponse.from_aggregate(service.cancel_task(task_id))


@router.post(
    "/api/v1/tasks/{task_id}/handoffs",
    response_model=HandoffResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["handoffs"],
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def request_handoff(
    task_id: UUID,
    payload: RequestHandoffRequest,
    service: HandoffServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> HandoffResponse:
    handoff = service.request_handoff(
        task_id=task_id,
        source_subtask_id=payload.source_subtask_id,
        target_subtask_id=payload.target_subtask_id,
        target_agent_id=payload.target_agent_id,
        objective=payload.objective,
        reason=payload.reason,
        completed_work_summary=payload.completed_work_summary,
        requested_by=principal.audit_actor(payload.requested_by),
        unresolved_questions=tuple(payload.unresolved_questions),
        constraints=payload.constraints,
        acceptance_criteria=tuple(payload.acceptance_criteria),
        idempotency_key=idempotency_key,
    )
    return HandoffResponse.from_domain(handoff)


@router.get(
    "/api/v1/tasks/{task_id}/handoffs/{handoff_id}",
    response_model=HandoffResponse,
    tags=["handoffs"],
    dependencies=[Depends(require_permission(Permission.TASK_READ))],
)
def get_handoff(
    task_id: UUID,
    handoff_id: UUID,
    service: HandoffServiceDependency,
) -> HandoffResponse:
    return HandoffResponse.from_domain(service.get_handoff(task_id, handoff_id))


@router.post(
    "/api/v1/tasks/{task_id}/handoffs/{handoff_id}/accept",
    response_model=HandoffResponse,
    tags=["handoffs"],
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def accept_handoff(
    task_id: UUID,
    handoff_id: UUID,
    payload: DecideHandoffRequest,
    service: HandoffServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> HandoffResponse:
    return HandoffResponse.from_domain(
        service.accept_handoff(
            task_id,
            handoff_id,
            actor=principal.audit_actor(payload.actor),
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/api/v1/tasks/{task_id}/handoffs/{handoff_id}/reject",
    response_model=HandoffResponse,
    tags=["handoffs"],
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def reject_handoff(
    task_id: UUID,
    handoff_id: UUID,
    payload: DecideHandoffRequest,
    service: HandoffServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader = None,
) -> HandoffResponse:
    return HandoffResponse.from_domain(
        service.reject_handoff(
            task_id,
            handoff_id,
            actor=principal.audit_actor(payload.actor),
            reason=payload.reason or "",
            idempotency_key=idempotency_key,
        )
    )
