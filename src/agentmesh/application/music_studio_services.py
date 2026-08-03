"""Compatibility imports for the Music Studio scenario runtime.

New integrations should import from :mod:`agentmesh.packs.music_studio.runtime`.
"""

from agentmesh.packs.music_studio.runtime import (
    AGENTS,
    WORKFLOW_KEY,
    MusicCandidateResult,
    MusicProjectLaunch,
    MusicProjectResult,
    MusicStudioService,
)

__all__ = [
    "AGENTS",
    "WORKFLOW_KEY",
    "MusicCandidateResult",
    "MusicProjectLaunch",
    "MusicProjectResult",
    "MusicStudioService",
]
