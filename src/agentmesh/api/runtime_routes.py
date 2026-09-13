from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.schemas import TaskResolutionResponse
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.runtime_integrity_services import (
    RuntimeIntegrityService,
)
from agentmesh.application.runtime_reconciliation import (
    RuntimeOutcomeReconciliationResult,
    RuntimeOutcomeReconciliationService,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import AuthorizationDenied, InvalidTaskInput
from agentmesh.domain.identity import Permission
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentStatus,
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


class RuntimeIntegrityIncidentResponse(BaseModel):
    id: UUID
    runtime_execution_id: UUID
    accepted_observation_id: str
    accepted_observation_digest: str
    accepted_phase: str
    conflicting_observation_id: str
    conflicting_observation_digest: str
    conflicting_phase: str
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime


class RuntimeIntegrityIncidentActionResponse(BaseModel):
    id: UUID
    incident_id: UUID
    action: str
    from_status: str
    to_status: str
    actor_principal_id: str
    reason: str
    request_digest: str
    created_at: datetime


class RuntimeIntegrityActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


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


def _integrity_service(request: Request) -> RuntimeIntegrityService:
    service = request.app.state.container.runtime_integrity_service
    if service is None:
        raise RuntimeError("Runtime integrity service is not configured")
    return service


RuntimeIntegrityServiceDependency = Annotated[
    RuntimeIntegrityService, Depends(_integrity_service)
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


def _integrity_incident(value: RuntimeIntegrityIncident) -> RuntimeIntegrityIncidentResponse:
    return RuntimeIntegrityIncidentResponse(
        id=value.id,
        runtime_execution_id=value.runtime_execution_id,
        accepted_observation_id=value.accepted_observation_id,
        accepted_observation_digest=value.accepted_observation_digest,
        accepted_phase=value.accepted_phase.value,
        conflicting_observation_id=value.conflicting_observation_id,
        conflicting_observation_digest=value.conflicting_observation_digest,
        conflicting_phase=value.conflicting_phase.value,
        status=value.status.value,
        reason=value.reason,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _integrity_action(
    value: RuntimeIntegrityIncidentAction,
) -> RuntimeIntegrityIncidentActionResponse:
    return RuntimeIntegrityIncidentActionResponse(
        id=value.id,
        incident_id=value.incident_id,
        action=value.action.value,
        from_status=value.from_status.value,
        to_status=value.to_status.value,
        actor_principal_id=value.actor_principal_id,
        reason=value.reason,
        request_digest=value.request_digest,
        created_at=value.created_at,
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


@router.get("/runtime-integrity-incidents", dependencies=_dependencies)
def list_runtime_integrity_incidents(
    service: RuntimeIntegrityServiceDependency,
    principal: PrincipalDependency,
    execution_id: UUID | None = None,
    status: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[RuntimeIntegrityIncidentResponse]:
    parsed_status = None
    if status is not None:
        try:
            parsed_status = RuntimeIntegrityIncidentStatus(status)
        except ValueError as exc:
            raise InvalidTaskInput("Runtime integrity incident status is invalid") from exc
    return [
        _integrity_incident(value)
        for value in service.list_incidents(
            principal=principal,
            execution_id=execution_id,
            status=parsed_status,
            limit=limit,
            offset=offset,
        )
    ]


@router.get("/runtime-integrity-incidents/{incident_id}", dependencies=_dependencies)
def get_runtime_integrity_incident(
    incident_id: UUID,
    service: RuntimeIntegrityServiceDependency,
    principal: PrincipalDependency,
) -> RuntimeIntegrityIncidentResponse:
    return _integrity_incident(service.get_incident(incident_id, principal=principal))


@router.get(
    "/runtime-integrity-incidents/{incident_id}/actions", dependencies=_dependencies
)
def list_runtime_integrity_incident_actions(
    incident_id: UUID,
    service: RuntimeIntegrityServiceDependency,
    principal: PrincipalDependency,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[RuntimeIntegrityIncidentActionResponse]:
    return [
        _integrity_action(value)
        for value in service.list_actions(
            incident_id, principal=principal, limit=limit, offset=offset
        )
    ]


@router.post(
    "/runtime-integrity-incidents/{incident_id}/acknowledge",
    dependencies=[
        *_dependencies,
        Depends(require_permission(Permission.OUTCOME_RECONCILE)),
    ],
)
def acknowledge_runtime_integrity_incident(
    incident_id: UUID,
    payload: RuntimeIntegrityActionRequest,
    service: RuntimeIntegrityServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyKey,
) -> RuntimeIntegrityIncidentResponse:
    result = service.acknowledge(
        incident_id,
        principal=principal,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return _integrity_incident(result.incident)


@router.post(
    "/runtime-integrity-incidents/{incident_id}/escalate",
    dependencies=[
        *_dependencies,
        Depends(require_permission(Permission.OUTCOME_RECONCILE)),
    ],
)
def escalate_runtime_integrity_incident(
    incident_id: UUID,
    payload: RuntimeIntegrityActionRequest,
    service: RuntimeIntegrityServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyKey,
) -> RuntimeIntegrityIncidentResponse:
    result = service.escalate(
        incident_id,
        principal=principal,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return _integrity_incident(result.incident)
