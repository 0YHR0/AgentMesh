from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import FastAPI

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import TaskApplicationService
from agentmesh.extensions.sdk import (
    CoreServiceKey,
    ExtensionContext,
    ExtensionHealth,
    ExtensionManifest,
    ExtensionServices,
    ExtensionWorkspace,
    RuntimeExtensionDefinition,
)
from agentmesh.features import Feature
from agentmesh.packs.music_studio.definition import DEFINITION, PACK_VERSION
from agentmesh.packs.music_studio.runtime import MusicStudioService

EXTENSION_ID = "agentmesh.music-studio"
SERVICE_KEY = "studio"


def _create_services(context: ExtensionContext) -> dict[str, object]:
    return {
        SERVICE_KEY: MusicStudioService(
            uow_factory=cast(
                UnitOfWorkFactory,
                context.require(CoreServiceKey.UNIT_OF_WORK_FACTORY),
            ),
            task_service=cast(
                TaskApplicationService,
                context.require(CoreServiceKey.TASKS),
            ),
            registry_service=cast(
                AgentRegistryService,
                context.require(CoreServiceKey.AGENT_REGISTRY),
            ),
            business_object_service=cast(
                BusinessObjectService,
                context.require(CoreServiceKey.BUSINESS_OBJECTS),
            ),
            artifact_service=cast(
                ArtifactService,
                context.require(CoreServiceKey.ARTIFACTS),
            ),
            tenant_id=context.tenant_id,
        )
    }


def _register_api(application: FastAPI) -> None:
    # Imports stay scenario-local and happen only while composing the HTTP surface.
    from agentmesh.packs.music_studio.console import register_music_studio_console
    from agentmesh.packs.music_studio.routes import router

    application.include_router(router)
    register_music_studio_console(application)


def _health(services: ExtensionServices) -> ExtensionHealth:
    services.require(SERVICE_KEY)
    asset_directory = Path(__file__).with_name("assets")
    expected = {"music-studio.html", "music-studio.css", "music-studio.js"}
    missing = sorted(name for name in expected if not (asset_directory / name).is_file())
    if missing:
        return ExtensionHealth(
            status="unhealthy",
            message="Missing workspace assets: " + ", ".join(missing),
        )
    return ExtensionHealth(status="ready")


EXTENSION = RuntimeExtensionDefinition(
    manifest=ExtensionManifest(
        identifier=EXTENSION_ID,
        name="AgentMesh Music Studio",
        version=PACK_VERSION,
        description="A governed multi-Agent studio for producing and reviewing original music.",
        required_core_services=(
            CoreServiceKey.UNIT_OF_WORK_FACTORY.value,
            CoreServiceKey.TASKS.value,
            CoreServiceKey.AGENT_REGISTRY.value,
            CoreServiceKey.BUSINESS_OBJECTS.value,
            CoreServiceKey.ARTIFACTS.value,
        ),
        provided_services=(SERVICE_KEY,),
        required_features=(
            Feature.ARTIFACT_SERVICE.value,
            Feature.COMPANY_MODEL.value,
            Feature.BUSINESS_OBJECTS.value,
            Feature.COMPANY_PACKS.value,
        ),
        permissions=("company:manage", "task:create", "task:operate"),
        workspaces=(
            ExtensionWorkspace(
                route="/music-studio",
                name="Music Studio",
                asset_prefix="/console/assets/music-studio",
            ),
        ),
        external_writes_enabled=False,
    ),
    service_factory=_create_services,
    api_registrar=_register_api,
    company_templates=(DEFINITION,),
    health_probe=_health,
)

__all__ = ["EXTENSION", "EXTENSION_ID", "SERVICE_KEY"]
