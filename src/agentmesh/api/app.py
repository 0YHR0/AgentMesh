from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentmesh.api.routes import router
from agentmesh.bootstrap import ApplicationContainer, build_runtime_container
from agentmesh.domain.errors import (
    ConcurrentTaskUpdate,
    InvalidTaskInput,
    InvalidTaskTransition,
    TaskExecutionFailed,
    TaskNotFound,
)


def create_app(container: ApplicationContainer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime_container = container or build_runtime_container()
        app.state.container = runtime_container
        try:
            yield
        finally:
            if container is None:
                runtime_container.close()

    application = FastAPI(
        title="AgentMesh Control API",
        version="0.1.0",
        description="Minimal durable single-agent vertical slice for AgentMesh.",
        lifespan=lifespan,
    )
    application.include_router(router)
    _register_error_handlers(application)
    return application


def _register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(TaskNotFound)
    async def handle_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "task_not_found", str(exc))

    @application.exception_handler(InvalidTaskInput)
    async def handle_invalid_input(request: Request, exc: InvalidTaskInput) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_task_input", str(exc))

    @application.exception_handler(InvalidTaskTransition)
    async def handle_invalid_transition(
        request: Request, exc: InvalidTaskTransition
    ) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "invalid_task_transition", str(exc))

    @application.exception_handler(ConcurrentTaskUpdate)
    async def handle_concurrent_update(
        request: Request, exc: ConcurrentTaskUpdate
    ) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "concurrent_task_update", str(exc))

    @application.exception_handler(TaskExecutionFailed)
    async def handle_execution_failed(
        request: Request, exc: TaskExecutionFailed
    ) -> JSONResponse:
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "task_execution_failed",
            str(exc),
        )


def _error(http_status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"code": code, "message": message})


app = create_app()
