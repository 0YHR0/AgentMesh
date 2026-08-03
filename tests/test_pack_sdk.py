import pytest

from agentmesh.domain.errors import InvalidCompanyPack
from agentmesh.packs.catalog import BUILTIN_PACK_CATALOG
from agentmesh.packs.music_studio import DEFINITION
from agentmesh.packs.sdk import PACK_SDK_API_VERSION, PackCatalog
from agentmesh.templates.music_studio import DEFINITION as LEGACY_DEFINITION


def test_music_studio_is_discoverable_through_stable_pack_sdk():
    discovered = BUILTIN_PACK_CATALOG.get("music-studio")

    assert discovered is DEFINITION
    assert discovered.sdk_api_version == PACK_SDK_API_VERSION
    assert discovered.build_pack().content_digest == DEFINITION.build_pack().content_digest
    assert discovered.required_credentials == ()
    assert not discovered.external_writes_enabled
    assert LEGACY_DEFINITION is DEFINITION


def test_pack_definition_owns_scenario_configuration_validation():
    configuration = DEFINITION.normalize_configuration(
        {
            "default_language": " zh-CN ",
            "default_genre": " dance-pop ",
            "use_plan": " PERSONAL ",
        }
    )

    assert configuration == {
        "default_language": "zh-CN",
        "default_genre": "dance-pop",
        "use_plan": "personal",
        "generation_provider": "deterministic-demo",
        "external_writes_enabled": False,
    }
    with pytest.raises(InvalidCompanyPack, match="Use plan"):
        DEFINITION.normalize_configuration(
            {
                "default_language": "en",
                "default_genre": "pop",
                "use_plan": "unbounded-publishing",
            }
        )


def test_pack_catalog_rejects_ambiguous_registration():
    catalog = PackCatalog([DEFINITION])

    with pytest.raises(InvalidCompanyPack, match="already registered"):
        catalog.register(DEFINITION)
