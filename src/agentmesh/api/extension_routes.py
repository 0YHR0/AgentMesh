from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agentmesh.extensions.runtime import RuntimeExtensionStatus

router = APIRouter(prefix="/api/v1/extensions", tags=["runtime-extensions"])


class ExtensionWorkspaceResponse(BaseModel):
    route: str
    name: str
    asset_prefix: str | None


class RuntimeExtensionResponse(BaseModel):
    identifier: str
    name: str
    version: str
    api_version: str
    description: str
    trust: str
    source: str | None
    distribution: str | None
    entry_point: str | None
    wheel_sha256: str | None
    lock_verified: bool
    enabled: bool
    health: str
    message: str
    required_features: list[str]
    missing_features: list[str]
    required_credentials: list[str]
    permissions: list[str]
    provided_services: list[str]
    loaded_services: list[str]
    workspaces: list[ExtensionWorkspaceResponse]
    external_writes_enabled: bool

    @classmethod
    def from_status(cls, status: RuntimeExtensionStatus) -> RuntimeExtensionResponse:
        manifest = status.manifest
        return cls(
            identifier=manifest.identifier,
            name=manifest.name,
            version=manifest.version,
            api_version=manifest.api_version,
            description=manifest.description,
            trust=status.trust.trust,
            source=status.trust.source,
            distribution=status.trust.distribution,
            entry_point=status.trust.entry_point,
            wheel_sha256=status.trust.wheel_sha256,
            lock_verified=status.trust.trust != "unmanaged",
            enabled=status.enabled,
            health=status.health,
            message=status.message,
            required_features=list(manifest.required_features),
            missing_features=list(status.missing_features),
            required_credentials=list(manifest.required_credentials),
            permissions=list(manifest.permissions),
            provided_services=list(manifest.provided_services),
            loaded_services=list(status.service_keys),
            workspaces=[
                ExtensionWorkspaceResponse(
                    route=item.route,
                    name=item.name,
                    asset_prefix=item.asset_prefix,
                )
                for item in manifest.workspaces
            ],
            external_writes_enabled=manifest.external_writes_enabled,
        )


@router.get("", response_model=list[RuntimeExtensionResponse])
def list_runtime_extensions(request: Request) -> list[RuntimeExtensionResponse]:
    return [
        RuntimeExtensionResponse.from_status(item)
        for item in request.app.state.container.extension_runtime.statuses()
    ]
