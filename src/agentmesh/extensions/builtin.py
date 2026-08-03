"""Composition catalog for built-in and installed trusted runtime extensions."""

from agentmesh.extensions.runtime import RuntimeExtensionRegistry
from agentmesh.packs.music_studio.extension import EXTENSION as MUSIC_STUDIO_EXTENSION

RUNTIME_EXTENSION_REGISTRY = RuntimeExtensionRegistry.discover((MUSIC_STUDIO_EXTENSION,))

__all__ = ["MUSIC_STUDIO_EXTENSION", "RUNTIME_EXTENSION_REGISTRY"]
