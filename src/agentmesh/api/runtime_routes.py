from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.schemas import TaskResolutionResponse
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.runtime_reconciliation import (
    RuntimeOutcomeReconciliationResult,
    RuntimeOutcomeReconciliationService,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import AuthorizationDenied, InvalidTaskInput
from agentmesh.domain.identity import Permission
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeRegistration,
    RuntimeVersion,
)
from agentmesh.features import Feature
from agentmesh.runtime_sdk import RuntimeContractError, RuntimeObservation

router = APIRouter(prefix="/api/v1", tags=["runtime-control-plane"])
_dependencies = [
    Depends(require_permission(Permission.RUNTIME_READ)),
    Depends(require_feature(Feature.MANAGED_AGENT_RUNTIME)),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class RuntimeRegistrationResponse(BaseModel):
    id: UUID
    tenant_id: str | None
    name: str
    visibility: str
    status: str
    default_version_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


class RuntimeVersionResponse(BaseModel):
    id: UUID
    runtime_id: UUID
    api_version: int
    adapter_kind: str
    artifact_digest: str
    configuration_digest: str
    trust_profile: str
    status: str
    created_at: datetime
    published_at: datetime | None


class RuntimeExecutionResponse(BaseModel):
    id: UUID
    run_id: UUID
    runtime_version_id: UUID
    assignment_id: UUID
    assignment_digest: str
    dispatch_key: str
    phase: str
    current_owner_attempt_id: UUID | None
    current_fencing_token: int | None
    provider_sequence: int | None
    provider_execution_ref_present: bool
    checkpoint_ref_present: bool
    workspace_ref_present: bool
    version: int
    updated_at: datetime
    terminal_at: datetime | None


class RuntimeObservationResponse(BaseModel):
    id: UUID
    observation_id: str
    observation_digest: str
    assignment_id: UUID
    assignment_digest: str
    provider_sequence: int | None
    phase: str
    observed_at: datetime
    received_at: datetime
    safe_summary: str | None
    processing_outcome: str
    provider_event_present: bool


class ReconcileRuntimeOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep the versioned Runtime contract opaque to Pydantic so malformed
    # values are normalized by the SDK boundary below instead of being echoed
    # in FastAPI's default validation response.
    observation: Any
    evidence_digest: str
    evidence_reference: str
    reason: str


class ReconcileRuntimeOutcomeResponse(BaseModel):
    execution: RuntimeExecutionResponse
    resolution: TaskResolutionResponse


def _service(request: Request) -> RuntimeRegistryService:
    service = request.app.state.container.runtime_service
    if service is None:
        raise RuntimeError("Runtime service is not configured")
    return service


RuntimeServiceDependency = Annotated[RuntimeRegistryService, Depends(_service)]


def _reconciliation_service(request: Request) -> RuntimeOutcomeReconciliationService:
    service = request.app.state.container.runtime_reconciliation_service
    if service is None:
        raise RuntimeError("Runtime reconciliation service is not configured")
    return service


RuntimeReconciliationServiceDependency = Annotated[
    RuntimeOutcomeReconciliationService, Depends(_reconciliation_service)
]


def _principal_uuid(principal: PrincipalDependency) -> UUID | None:
    try:
        return UUID(principal.principal_id)
    except (TypeError, ValueError):
        return None


def _assert_tenant(service: RuntimeRegistryService, principal: PrincipalDependency) -> None:
    if principal.tenant_id != service.tenant_id:
        raise AuthorizationDenied("Runtime tenant scope denied")


def _registration(value: RuntimeRegistration) -> RuntimeRegistrationResponse:
    return RuntimeRegistrationResponse(
        id=value.id,
        tenant_id=value.tenant_id,
        name=value.name,
        visibility=value.visibility.value,
        status=value.status.value,
        default_version_id=value.default_version_id,
        version=value.version,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _version(value: RuntimeVersion) -> RuntimeVersionResponse:
    return RuntimeVersionResponse(
        id=value.id,
        runtime_id=value.runtime_id,
        api_version=value.api_version,
        adapter_kind=value.adapter_kind,
        artifact_digest=value.artifact_digest,
        configuration_digest=value.configuration_digest,
        trust_profile=value.trust_profile.value,
        status=value.status.value,
        created_at=value.created_at,
        published_at=value.published_at,
    )


def _execution(value: RuntimeExecution) -> RuntimeExecutionResponse:
    return RuntimeExecutionResponse(
        id=value.id,
        run_id=value.run_id,
        runtime_version_id=value.runtime_version_id,
        assignment_id=value.assignment_id,
        assignment_digest=value.assignment_digest,
        dispatch_key=value.dispatch_key,
        phase=value.phase.value,
        current_owner_attempt_id=value.current_owner_attempt_id,
        current_fencing_token=value.current_fencing_token,
        provider_sequence=value.provider_sequence,
        provider_execution_ref_present=value.provider_execution_ref is not None,
        checkpoint_ref_present=value.checkpoint_ref is not None,
        workspace_ref_present=value.workspace_ref is not None,
        version=value.version,
        updated_at=value.updated_at,
        terminal_at=value.terminal_at,
    )


@router.get("/runtimes", dependencies=_dependencies)
def list_runtimes(
    service: RuntimeServiceDependency,
    principal: PrincipalDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[RuntimeRegistrationResponse]:
    _assert_tenant(service, principal)
    return [
        _registration(value)
        for value in service.list_registrations(
            limit=limit, offset=offset, principal_id=_principal_uuid(principal)
        )
    ]


@router.get("/runtimes/{runtime_id}/versions", dependencies=_dependencies)
def list_versions(
    runtime_id: UUID, service: RuntimeServiceDependency, principal: PrincipalDependency
) -> list[RuntimeVersionResponse]:
    _assert_tenant(service, principal)
    return [
        _version(value)
        for value in service.list_versions(runtime_id, principal_id=_principal_uuid(principal))
    ]


@router.get("/runtime-executions/{execution_id}", dependencies=_dependencies)
def get_execution(
    execution_id: UUID, service: RuntimeServiceDependency, principal: PrincipalDependency
) -> RuntimeExecutionResponse:
    _assert_tenant(service, principal)
    return _execution(service.get_execution(execution_id))


@router.get("/runtime-executions/{execution_id}/observations", dependencies=_dependencies)
def list_observations(
    execution_id: UUID,
    service: RuntimeServiceDependency,
    principal: PrincipalDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[RuntimeObservationResponse]:
    _assert_tenant(service, principal)
    return [
        RuntimeObservationResponse(**value)
        for value in service.list_observations(execution_id, limit=limit, offset=offset)
    ]


@router.post(
    "/runtime-executions/{execution_id}/reconcile-outcome",
    response_model=ReconcileRuntimeOutcomeResponse,
    dependencies=[
        *_dependencies,
        Depends(require_feature(Feature.OUTCOME_RECONCILIATION)),
        Depends(require_permission(Permission.OUTCOME_RECONCILE)),
    ],
)
def reconcile_runtime_outcome(
    execution_id: UUID,
    payload: ReconcileRuntimeOutcomeRequest,
    principal: PrincipalDependency,
    service: RuntimeReconciliationServiceDependency,
    idempotency_key: IdempotencyKey,
) -> ReconcileRuntimeOutcomeResponse:
    if principal.tenant_id != service.tenant_id or not principal.authenticated:
        raise AuthorizationDenied("Runtime tenant scope denied")
    try:
        observation = RuntimeObservation.from_dict(payload.observation)
    except RuntimeContractError as exc:
        # Runtime contract errors can contain field-level details derived from an
        # untrusted request.  Keep the public error stable and bounded while the
        # domain exception handler maps it to HTTP 422.
        raise InvalidTaskInput(
            "Runtime reconciliation observation is invalid"
        ) from exc
    result: RuntimeOutcomeReconciliationResult = service.reconcile_outcome(
        execution_id,
        principal=principal,
        observation=observation,
        evidence_digest=payload.evidence_digest,
        evidence_reference=payload.evidence_reference,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return ReconcileRuntimeOutcomeResponse(
        execution=_execution(result.execution),
        resolution=TaskResolutionResponse.from_domain(result.resolution),
    )
