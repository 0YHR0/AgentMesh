from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentmesh.api.agent_routes import router as agent_router
from agentmesh.api.feature_routes import router as feature_router
from agentmesh.api.routes import router
from agentmesh.bootstrap import ApplicationContainer, build_api_container
from agentmesh.domain.errors import (
    AgentDefinitionNotFound,
    AgentDeploymentNotFound,
    AgentRegistryConflict,
    AgentUnavailable,
    AgentVersionNotFound,
    CapabilityNotFound,
    ConcurrentTaskUpdate,
    FeatureDisabled,
    IdempotencyConflict,
    InvalidAgentDefinition,
    InvalidAgentTransition,
    InvalidAgentVersion,
    InvalidTaskInput,
    InvalidTaskTransition,
    TaskExecutionFailed,
    TaskNotFound,
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
    application.include_router(agent_router)
    _register_error_handlers(application)
    return application


def _register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(FeatureDisabled)
    async def handle_feature_disabled(request: Request, exc: FeatureDisabled) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, "feature_disabled", str(exc))

    @application.exception_handler(TaskNotFound)
    async def handle_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "task_not_found", str(exc))

    @application.exception_handler(InvalidTaskInput)
    async def handle_invalid_input(request: Request, exc: InvalidTaskInput) -> JSONResponse:
        return _error(422, "invalid_task_input", str(exc))

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
