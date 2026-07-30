from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.organizational_memory_schemas import (
    CreateMemoryPolicyRequest,
    MemoryPolicyResponse,
    MemoryRetrievalResponse,
    MemorySearchResponse,
    MemorySnapshotResponse,
    ProposeMemoryRequest,
    ReviewMemoryRequest,
    RevokeMemoryRequest,
    SearchMemoryRequest,
)
from agentmesh.api.security import (
    PrincipalDependency,
    require_permission,
    require_read_or_write_permission,
)
from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
)
from agentmesh.domain.identity import Permission
from agentmesh.domain.organizational_memory import MemoryStatus
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/companies/{company_id}/memory",
    tags=["organizational-memory"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        ),
        Depends(require_feature(Feature.ORGANIZATIONAL_MEMORY)),
    ],
)


def get_service(request: Request) -> OrganizationalMemoryService:
    return request.app.state.container.organizational_memory_service


ServiceDependency = Annotated[
    OrganizationalMemoryService, Depends(get_service)
]


@router.post(
    "/policies",
    response_model=MemoryPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_policy(
    company_id: UUID,
    payload: CreateMemoryPolicyRequest,
    service: ServiceDependency,
) -> MemoryPolicyResponse:
    return MemoryPolicyResponse.model_validate(
        service.create_policy(company_id, **payload.model_dump())
    )


@router.get("/policies", response_model=list[MemoryPolicyResponse])
def list_policies(
    company_id: UUID, service: ServiceDependency
) -> list[MemoryPolicyResponse]:
    return [
        MemoryPolicyResponse.model_validate(value)
        for value in service.list_policies(company_id)
    ]


@router.post(
    "/candidates",
    response_model=MemorySnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose(
    company_id: UUID,
    payload: ProposeMemoryRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MemorySnapshotResponse:
    values = payload.model_dump()
    values["evidence"] = [
        item.model_dump() for item in payload.evidence
    ]
    return MemorySnapshotResponse.from_snapshot(
        service.propose(
            company_id,
            **values,
            actor=principal.principal_id,
        )
    )


@router.get(
    "/candidates",
    response_model=list[MemorySnapshotResponse],
    dependencies=[Depends(require_permission(Permission.COMPANY_MANAGE))],
)
def list_candidates(
    company_id: UUID, service: ServiceDependency
) -> list[MemorySnapshotResponse]:
    return [
        MemorySnapshotResponse.from_snapshot(value)
        for value in service.list_candidates(company_id)
    ]


@router.get(
    "/records",
    response_model=list[MemorySnapshotResponse],
    dependencies=[Depends(require_permission(Permission.COMPANY_MANAGE))],
)
def list_memories(
    company_id: UUID,
    service: ServiceDependency,
    memory_status: Annotated[list[MemoryStatus] | None, Query(alias="status")] = None,
) -> list[MemorySnapshotResponse]:
    statuses = set(memory_status) if memory_status else None
    return [
        MemorySnapshotResponse.from_snapshot(value)
        for value in service.list_memories(company_id, statuses=statuses)
    ]


@router.post("/_search", response_model=MemorySearchResponse)
def search(
    company_id: UUID,
    payload: SearchMemoryRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MemorySearchResponse:
    values = payload.model_dump(exclude={"namespaces"})
    values["namespaces"] = [
        (item.namespace_type, item.namespace_id) for item in payload.namespaces
    ]
    return MemorySearchResponse.from_result(
        service.search(
            company_id,
            **values,
            principal_id=principal.principal_id,
        )
    )


@router.get("/_retrievals", response_model=list[MemoryRetrievalResponse])
def list_retrievals(
    company_id: UUID,
    service: ServiceDependency,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
) -> list[MemoryRetrievalResponse]:
    return [
        MemoryRetrievalResponse.model_validate(value)
        for value in service.list_retrievals(
            company_id, task_id=task_id, run_id=run_id
        )
    ]


@router.get(
    "/{memory_id}",
    response_model=MemorySnapshotResponse,
    dependencies=[Depends(require_permission(Permission.COMPANY_MANAGE))],
)
def get_memory(
    company_id: UUID, memory_id: UUID, service: ServiceDependency
) -> MemorySnapshotResponse:
    return MemorySnapshotResponse.from_snapshot(
        service.get_memory(company_id, memory_id)
    )


@router.post("/{memory_id}/review", response_model=MemorySnapshotResponse)
def review_memory(
    company_id: UUID,
    memory_id: UUID,
    payload: ReviewMemoryRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MemorySnapshotResponse:
    return MemorySnapshotResponse.from_snapshot(
        service.review(
            company_id,
            memory_id,
            **payload.model_dump(),
            reviewer=principal.principal_id,
            reviewer_roles={role.value for role in principal.roles},
        )
    )


@router.post("/{memory_id}/revoke", response_model=MemorySnapshotResponse)
def revoke_memory(
    company_id: UUID,
    memory_id: UUID,
    payload: RevokeMemoryRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MemorySnapshotResponse:
    return MemorySnapshotResponse.from_snapshot(
        service.revoke(
            company_id,
            memory_id,
            reviewer=principal.principal_id,
            reason=payload.reason,
        )
    )
