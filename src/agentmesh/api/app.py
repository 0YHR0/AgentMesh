from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentmesh.api.a2a_routes import router as a2a_router
from agentmesh.api.agent_routes import router as agent_router
from agentmesh.api.artifact_routes import router as artifact_router
from agentmesh.api.console import register_console
from agentmesh.api.credential_routes import router as credential_router
from agentmesh.api.event_routes import router as event_router
from agentmesh.api.feature_routes import router as feature_router
from agentmesh.api.identity_routes import admin_router as identity_admin_router
from agentmesh.api.identity_routes import router as identity_router
from agentmesh.api.mcp_routes import registry_router as mcp_registry_router
from agentmesh.api.mcp_routes import router as mcp_router
from agentmesh.api.policy_routes import router as policy_router
from agentmesh.api.quota_routes import router as quota_router
from agentmesh.api.routes import router
from agentmesh.bootstrap import ApplicationContainer, build_api_container
from agentmesh.domain.errors import (
    A2ADelegationConflict,
    A2ADelegationNotFound,
    A2ARegistryConflict,
    A2ARegistryNotFound,
    AgentDefinitionNotFound,
    AgentDeploymentNotFound,
    AgentRegistryConflict,
    AgentUnavailable,
    AgentVersionNotFound,
    ArtifactIntegrityMismatch,
    ArtifactNotFound,
    ArtifactTooLarge,
    ArtifactVersionNotFound,
    AuthenticationFailed,
    AuthenticationRequired,
    AuthorizationDenied,
    CapabilityNotFound,
    ConcurrentTaskUpdate,
    CredentialConflict,
    CredentialNotFound,
    CredentialProviderUnavailable,
    ExecutionPermitRequired,
    FeatureDisabled,
    GovernedActionNotFound,
    HandoffNotFound,
    IdempotencyConflict,
    IdentityConflict,
    InvalidA2ADelegation,
    InvalidA2ADelegationTransition,
    InvalidA2ARegistry,
    InvalidA2ATransition,
    InvalidAgentDefinition,
    InvalidAgentTransition,
    InvalidAgentVersion,
    InvalidArtifact,
    InvalidCredential,
    InvalidIdentity,
    InvalidMcpRegistry,
    InvalidMcpTransition,
    InvalidPolicyTransition,
    InvalidTaskInput,
    InvalidTaskTransition,
    InvalidToolRequest,
    McpRegistryConflict,
    McpRegistryNotFound,
    PlanPatchNotFound,
    PrincipalNotFound,
    RoleBindingNotFound,
    TaskExecutionFailed,
    TaskNotFound,
    ToolInvocationFailed,
)


def create_app(container: ApplicationContainer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime_container = container or build_api_container()
        app.state.container = runtime_container
        try:
            yield
        finally:
            if container is None:
                runtime_container.close()

    application = FastAPI(
        title="AgentMesh Control API",
        version="0.1.0",
        description="Durable asynchronous control API for AgentMesh.",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.include_router(feature_router)
    application.include_router(identity_router)
    application.include_router(identity_admin_router)
    application.include_router(agent_router)
    application.include_router(a2a_router)
    application.include_router(credential_router)
    application.include_router(event_router)
    application.include_router(artifact_router)
    application.include_router(mcp_router)
    application.include_router(mcp_registry_router)
    application.include_router(policy_router)
    application.include_router(quota_router)
    register_console(application)
    _register_error_handlers(application)
    return application


def _register_error_handlers(application: FastAPI) -> None:
    for error_type in (AuthenticationRequired, AuthenticationFailed):
        application.add_exception_handler(
            error_type,
            lambda request, exc: JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "code": "authentication_failed",
                    "message": "Bearer authentication failed",
                },
                headers={"WWW-Authenticate": "Bearer"},
            ),
        )

    application.add_exception_handler(
        AuthorizationDenied,
        lambda request, exc: _error(403, "authorization_denied", str(exc)),
    )
    application.add_exception_handler(
        GovernedActionNotFound,
        lambda request, exc: _error(404, "governed_action_not_found", str(exc)),
    )
    application.add_exception_handler(
        ExecutionPermitRequired,
        lambda request, exc: _error(403, "execution_permit_required", str(exc)),
    )
    application.add_exception_handler(
        InvalidPolicyTransition,
        lambda request, exc: _error(409, "invalid_policy_transition", str(exc)),
    )
    for error_type in (PrincipalNotFound, RoleBindingNotFound):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(404, "identity_not_found", str(exc)),
        )
    application.add_exception_handler(
        InvalidIdentity,
        lambda request, exc: _error(422, "invalid_identity", str(exc)),
    )
    application.add_exception_handler(
        PlanPatchNotFound,
        lambda request, exc: _error(404, "plan_patch_not_found", str(exc)),
    )
    application.add_exception_handler(
        IdentityConflict,
        lambda request, exc: _error(409, "identity_conflict", str(exc)),
    )
    application.add_exception_handler(
        McpRegistryNotFound,
        lambda request, exc: _error(404, "mcp_registry_not_found", str(exc)),
    )
    application.add_exception_handler(
        InvalidMcpRegistry,
        lambda request, exc: _error(422, "invalid_mcp_registry", str(exc)),
    )
    application.add_exception_handler(
        McpRegistryConflict,
        lambda request, exc: _error(409, "mcp_registry_conflict", str(exc)),
    )
    application.add_exception_handler(
        InvalidMcpTransition,
        lambda request, exc: _error(409, "invalid_mcp_transition", str(exc)),
    )
    application.add_exception_handler(
        A2ARegistryNotFound,
        lambda request, exc: _error(404, "a2a_registry_not_found", str(exc)),
    )
    application.add_exception_handler(
        InvalidA2ARegistry,
        lambda request, exc: _error(422, "invalid_a2a_registry", str(exc)),
    )
    for error_type in (InvalidA2ATransition, A2ARegistryConflict):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(409, "a2a_registry_conflict", str(exc)),
        )
    application.add_exception_handler(
        A2ADelegationNotFound,
        lambda request, exc: _error(404, "a2a_delegation_not_found", str(exc)),
    )
    application.add_exception_handler(
        InvalidA2ADelegation,
        lambda request, exc: _error(422, "invalid_a2a_delegation", str(exc)),
    )
    for error_type in (InvalidA2ADelegationTransition, A2ADelegationConflict):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(409, "a2a_delegation_conflict", str(exc)),
        )
    application.add_exception_handler(
        CredentialNotFound,
        lambda request, exc: _error(404, "credential_not_found", str(exc)),
    )
    application.add_exception_handler(
        InvalidCredential,
        lambda request, exc: _error(422, "invalid_credential", str(exc)),
    )
    application.add_exception_handler(
        CredentialProviderUnavailable,
        lambda request, exc: _error(503, "credential_provider_unavailable", str(exc)),
    )
    application.add_exception_handler(
        CredentialConflict,
        lambda request, exc: _error(409, "credential_conflict", str(exc)),
    )

    @application.exception_handler(FeatureDisabled)
    async def handle_feature_disabled(request: Request, exc: FeatureDisabled) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, "feature_disabled", str(exc))

    @application.exception_handler(TaskNotFound)
    async def handle_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "task_not_found", str(exc))

    @application.exception_handler(HandoffNotFound)
    async def handle_handoff_not_found(request: Request, exc: HandoffNotFound) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "handoff_not_found", str(exc))

    for error_type in (ArtifactNotFound, ArtifactVersionNotFound):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(404, "artifact_not_found", str(exc)),
        )

    for error_type in (InvalidArtifact, ArtifactIntegrityMismatch):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(422, "invalid_artifact", str(exc)),
        )

    application.add_exception_handler(
        ArtifactTooLarge,
        lambda request, exc: _error(413, "artifact_too_large", str(exc)),
    )

    @application.exception_handler(InvalidTaskInput)
    async def handle_invalid_input(request: Request, exc: InvalidTaskInput) -> JSONResponse:
        return _error(422, "invalid_task_input", str(exc))

    @application.exception_handler(InvalidToolRequest)
    async def handle_invalid_tool_request(
        request: Request, exc: InvalidToolRequest
    ) -> JSONResponse:
        return _error(422, "invalid_tool_request", str(exc))

    application.add_exception_handler(
        ToolInvocationFailed,
        lambda request, exc: _error(409, "tool_invocation_conflict", str(exc)),
    )

    @application.exception_handler(InvalidTaskTransition)
    async def handle_invalid_transition(
        request: Request, exc: InvalidTaskTransition
    ) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "invalid_task_transition", str(exc))

    @application.exception_handler(ConcurrentTaskUpdate)
    async def handle_concurrent_update(request: Request, exc: ConcurrentTaskUpdate) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "concurrent_task_update", str(exc))

    @application.exception_handler(IdempotencyConflict)
    async def handle_idempotency_conflict(
        request: Request, exc: IdempotencyConflict
    ) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "idempotency_conflict", str(exc))

    @application.exception_handler(TaskExecutionFailed)
    async def handle_execution_failed(request: Request, exc: TaskExecutionFailed) -> JSONResponse:
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "task_execution_failed",
            str(exc),
        )

    for error_type in (
        AgentDefinitionNotFound,
        AgentVersionNotFound,
        AgentDeploymentNotFound,
        CapabilityNotFound,
    ):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(404, "agent_registry_not_found", str(exc)),
        )

    for error_type in (InvalidAgentDefinition, InvalidAgentVersion):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(422, "invalid_agent_registry_input", str(exc)),
        )

    for error_type in (
        InvalidAgentTransition,
        AgentRegistryConflict,
        AgentUnavailable,
    ):
        application.add_exception_handler(
            error_type,
            lambda request, exc: _error(409, "agent_registry_conflict", str(exc)),
        )


def _error(http_status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"code": code, "message": message})


app = create_app()
