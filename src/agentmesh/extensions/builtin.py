"""Composition catalog for built-in and installed trusted runtime extensions."""

from agentmesh.extensions.runtime import RuntimeExtensionRegistry
from agentmesh.extensions.trust import load_configured_extension_lock
from agentmesh.packs.music_studio.extension import EXTENSION as MUSIC_STUDIO_EXTENSION

EXTENSION_LOCK = load_configured_extension_lock()
RUNTIME_EXTENSION_REGISTRY = RuntimeExtensionRegistry.discover(
    (MUSIC_STUDIO_EXTENSION,),
    lock=EXTENSION_LOCK,
)

__all__ = ["EXTENSION_LOCK", "MUSIC_STUDIO_EXTENSION", "RUNTIME_EXTENSION_REGISTRY"]
