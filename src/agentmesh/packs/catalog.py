"""Built-in scenario discovery.

Third-party repositories can construct their own ``PackCatalog`` with the same SDK.
"""

from agentmesh.packs.music_studio import DEFINITION as MUSIC_STUDIO
from agentmesh.packs.sdk import PackCatalog

BUILTIN_PACK_CATALOG = PackCatalog([MUSIC_STUDIO])

__all__ = ["BUILTIN_PACK_CATALOG", "MUSIC_STUDIO"]
