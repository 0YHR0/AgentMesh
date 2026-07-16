from agentmesh.config import Settings, get_settings


def test_cached_settings_factory_builds_settings() -> None:
    get_settings.cache_clear()

    settings = get_settings()

    assert isinstance(settings, Settings)
    get_settings.cache_clear()
