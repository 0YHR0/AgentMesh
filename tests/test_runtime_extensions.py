from collections.abc import Mapping
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.extensions.builtin import RUNTIME_EXTENSION_REGISTRY
from agentmesh.extensions.runtime import ExtensionRuntime, RuntimeExtensionRegistry
from agentmesh.extensions.sdk import (
    ExtensionContext,
    ExtensionHealth,
    ExtensionManifest,
    ExtensionServices,
    ExtensionWorkspace,
    InvalidRuntimeExtension,
    RuntimeExtensionDefinition,
    RuntimeExtensionUnavailable,
)
from agentmesh.features import Feature, FeatureGateSet


def _definition(
    *,
    identifier: str = "example.extension",
    route: str = "/example",
    factory: object | None = None,
    stop: object | None = None,
) -> RuntimeExtensionDefinition:
    def create_services(_: ExtensionContext) -> Mapping[str, object]:
        if callable(factory):
            return factory()
        return {"worker": object()}

    def register_api(_: FastAPI) -> None:
        return None

    def stop_services(services: ExtensionServices) -> None:
        if callable(stop):
            stop(services)

    return RuntimeExtensionDefinition(
        manifest=ExtensionManifest(
            identifier=identifier,
            name="Example",
            version="1.2.3",
            description="Example trusted extension",
            required_core_services=("tasks",),
            provided_services=("worker",),
            required_features=(Feature.ARTIFACT_SERVICE.value,),
            workspaces=(ExtensionWorkspace(route=route, name="Example"),),
        ),
        service_factory=create_services,
        api_registrar=register_api,
        health_probe=lambda _: ExtensionHealth(status="ready"),
        stop_callback=stop_services,
    )


def test_manifest_and_registry_reject_incompatible_or_ambiguous_extensions() -> None:
    with pytest.raises(InvalidRuntimeExtension, match="Unsupported runtime extension API"):
        ExtensionManifest(
            identifier="example.invalid",
            name="Invalid",
            version="1.0.0",
            description="Invalid API",
            required_core_services=(),
            provided_services=("worker",),
            api_version="9.9",
        )

    registry = RuntimeExtensionRegistry((_definition(),))
    with pytest.raises(InvalidRuntimeExtension, match="workspace route"):
        registry.register(_definition(identifier="another.extension"))


def test_registry_discovers_installed_trusted_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _definition()

    class FakeEntryPoint:
        name = "example"

        @staticmethod
        def load() -> RuntimeExtensionDefinition:
            return definition

    monkeypatch.setattr(
        "agentmesh.extensions.runtime.entry_points",
        lambda *, group: [FakeEntryPoint()] if group == "agentmesh.runtime_extensions" else [],
    )

    registry = RuntimeExtensionRegistry.discover()
    assert registry.get("example.extension") is definition


def test_api_registration_rejects_core_route_collisions() -> None:
    application = FastAPI()

    @application.get("/health")
    def core_health() -> dict[str, str]:
        return {"status": "ok"}

    def conflicting_api(app: FastAPI) -> None:
        @app.get("/health")
        def extension_health() -> dict[str, str]:
            return {"status": "conflict"}

    definition = replace(
        _definition(route="/health"),
        api_registrar=conflicting_api,
    )
    registry = RuntimeExtensionRegistry((definition,))
    with pytest.raises(InvalidRuntimeExtension, match="conflicting route '/health'"):
        registry.register_api(application)


def test_runtime_loads_namespaced_services_reports_features_and_stops_once() -> None:
    stopped: list[tuple[str, ...]] = []
    registry = RuntimeExtensionRegistry(
        (_definition(stop=lambda services: stopped.append(services.keys())),)
    )
    runtime = ExtensionRuntime.load(
        registry,
        ExtensionContext(tenant_id="tenant", services={"tasks": object()}),
        FeatureGateSet.from_config("minimal"),
        "example.extension",
    )

    assert runtime.require_service("example.extension", "worker") is not None
    status = runtime.statuses()[0]
    assert status.enabled is True
    assert status.health == "degraded"
    assert status.missing_features == (Feature.ARTIFACT_SERVICE.value,)
    assert status.service_keys == ("worker",)

    runtime.close()
    runtime.close()
    assert stopped == [("worker",)]


def test_disabled_and_unknown_extensions_fail_closed() -> None:
    registry = RuntimeExtensionRegistry((_definition(),))
    disabled = ExtensionRuntime.load(
        registry,
        ExtensionContext(tenant_id="tenant", services={}),
        FeatureGateSet.from_config("minimal"),
        "",
    )
    assert disabled.statuses()[0].health == "disabled"
    with pytest.raises(RuntimeExtensionUnavailable, match="disabled"):
        disabled.require_service("example.extension", "worker")

    with pytest.raises(InvalidRuntimeExtension, match="Unknown enabled"):
        ExtensionRuntime.load(
            registry,
            ExtensionContext(tenant_id="tenant", services={}),
            FeatureGateSet.from_config("minimal"),
            "missing.extension",
        )


def test_service_factory_must_match_declared_surface() -> None:
    registry = RuntimeExtensionRegistry(
        (_definition(factory=lambda: {"unexpected": object()}),)
    )
    with pytest.raises(InvalidRuntimeExtension, match="must provide exactly"):
        ExtensionRuntime.load(
            registry,
            ExtensionContext(tenant_id="tenant", services={"tasks": object()}),
            FeatureGateSet.from_config("minimal"),
            "example.extension",
        )


def test_extension_api_discloses_status_and_disabled_workspace_fails_closed(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        response = client.get("/api/v1/extensions")
        assert response.status_code == 200
        music = response.json()[0]
        assert music["identifier"] == "agentmesh.music-studio"
        assert music["trust"] == "built-in"
        assert music["lock_verified"] is True
        assert music["source"] == "https://github.com/0YHR0/AgentMesh"
        assert music["enabled"] is True
        assert music["provided_services"] == ["studio"]
        assert music["loaded_services"] == ["studio"]
        assert music["workspaces"][0]["route"] == "/music-studio"

    application_container.extension_runtime = ExtensionRuntime.load(
        RUNTIME_EXTENSION_REGISTRY,
        ExtensionContext(tenant_id="test-tenant", services={}),
        application_container.feature_gates,
        "",
    )
    with TestClient(create_app(application_container)) as client:
        status = client.get("/api/v1/extensions").json()[0]
        assert status["health"] == "disabled"
        assert status["provided_services"] == ["studio"]
        assert status["loaded_services"] == []
        disabled = client.get("/music-studio")
        assert disabled.status_code == 503
        assert disabled.json()["code"] == "runtime_extension_unavailable"
