"""Compatibility imports for the Music Studio HTTP API."""

from agentmesh.packs.music_studio.routes import (
    CreateMusicProjectRequest,
    MusicCandidateResponse,
    MusicProjectLaunchResponse,
    MusicProjectResultResponse,
    RequestMusicRevisionRequest,
    SelectMusicCandidateRequest,
    ServiceDependency,
    get_service,
    router,
)

__all__ = [
    "CreateMusicProjectRequest",
    "MusicCandidateResponse",
    "MusicProjectLaunchResponse",
    "MusicProjectResultResponse",
    "RequestMusicRevisionRequest",
    "SelectMusicCandidateRequest",
    "ServiceDependency",
    "get_service",
    "router",
]
