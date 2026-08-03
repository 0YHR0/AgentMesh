from agentmesh.api.music_studio_routes import router as compatibility_router
from agentmesh.application.music_studio_services import (
    MusicStudioService as CompatibilityMusicStudioService,
)
from agentmesh.integrations.music.deterministic import (
    DeterministicMusicProvider as CompatibilityMusicProvider,
)
from agentmesh.packs.music_studio.providers.deterministic import DeterministicMusicProvider
from agentmesh.packs.music_studio.routes import router
from agentmesh.packs.music_studio.runtime import MusicStudioService


def test_legacy_music_studio_imports_resolve_to_pack_owned_implementations() -> None:
    assert CompatibilityMusicStudioService is MusicStudioService
    assert CompatibilityMusicProvider is DeterministicMusicProvider
    assert compatibility_router is router
