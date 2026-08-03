"""Built-in scenario discovery.

Third-party repositories can construct their own ``PackCatalog`` with the same SDK.
"""

from agentmesh.extensions.builtin import RUNTIME_EXTENSION_REGISTRY
from agentmesh.packs.sdk import PackCatalog

_DEFINITIONS = tuple(
    template
    for extension in RUNTIME_EXTENSION_REGISTRY.list()
    for template in extension.company_templates
)
BUILTIN_PACK_CATALOG = PackCatalog(_DEFINITIONS)
MUSIC_STUDIO = BUILTIN_PACK_CATALOG.get("music-studio")

__all__ = ["BUILTIN_PACK_CATALOG", "MUSIC_STUDIO"]
