"""Trusted in-process runtime extension protocol."""

from agentmesh.extensions.sdk import (
    RUNTIME_EXTENSION_API_VERSION,
    CoreServiceKey,
    ExtensionContext,
    ExtensionHealth,
    ExtensionManifest,
    ExtensionServices,
    ExtensionWorkspace,
    RuntimeExtensionDefinition,
)

__all__ = [
    "RUNTIME_EXTENSION_API_VERSION",
    "CoreServiceKey",
    "ExtensionContext",
    "ExtensionHealth",
    "ExtensionManifest",
    "ExtensionServices",
    "ExtensionWorkspace",
    "RuntimeExtensionDefinition",
]
