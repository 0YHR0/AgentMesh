import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.domain.errors import CompanyPackConflict, InvalidCompanyPack
from agentmesh.features import FeatureGateSet
from agentmesh.templates.music_studio import build_pack, manifest


def test_music_studio_pack_is_stable_minimal_and_safe():
    first = build_pack()
    second = build_pack()
    resources = manifest()["resources"]

    assert first.content_digest == second.content_digest
    assert first.key == "agentmesh.music-studio"
    assert first.required_features == ["business_objects", "company_model"]
    assert sum(item["kind"] == "organization_unit" for item in resources) == 5
    assert sum(item["kind"] == "position" for item in resources) == 7
    assert sum(item["kind"] == "business_object_type" for item in resources) == 7
    assert manifest()["template"]["safety"] == {
        "external_writes_enabled": False,
        "artist_imitation_enabled": False,
        "voice_cloning_enabled": False,
        "distribution_enabled": False,
    }


def test_music_studio_template_provisions_company_atomically(
    company_pack_service,
    uow_factory,
):
    preview = company_pack_service.preview_music_studio_template()

    assert preview.installable
    assert preview.required_credentials == []
    assert preview.permissions == ["company:manage"]
    assert not preview.external_writes_enabled
    assert preview.resource_summary == {
        "organization_unit": 5,
        "position": 7,
        "business_object_type": 7,
    }

    result = company_pack_service.install_music_studio_template(
        company_name="North Star Music",
        owner_principal_id="owner",
        default_language="zh-CN",
        default_genre="dance-pop",
        use_plan="internal-demo",
        operating_timezone="Asia/Shanghai",
    )

    assert result.company.name == "North Star Music"
    assert result.installation.pack_digest == preview.content_digest
    assert result.installation.configuration == {
        "default_language": "zh-CN",
        "default_genre": "dance-pop",
        "use_plan": "internal-demo",
        "generation_provider": "deterministic-demo",
        "external_writes_enabled": False,
    }
    assert len(result.installation.resource_refs) == 19
    with uow_factory() as uow:
        assert len(uow.company_model.list_units(result.company.id)) == 5
        assert len(uow.company_model.list_positions(result.company.id)) == 7
        assert len(uow.company_packs.list_installations(result.company.id)) == 1
        assert (
            uow.business_objects.get_type_by_key(
                result.company.id,
                "final-release-package",
                published_only=True,
            )
            is not None
        )

    with pytest.raises(CompanyPackConflict, match="active Company"):
        company_pack_service.install_music_studio_template(
            company_name="Duplicate Music Studio",
            owner_principal_id="owner",
            default_language="en",
            default_genre="pop",
            use_plan="personal",
        )


def test_music_studio_template_rejects_invalid_configuration(company_pack_service):
    with pytest.raises(InvalidCompanyPack, match="Use plan"):
        company_pack_service.install_music_studio_template(
            company_name="Invalid Music Studio",
            owner_principal_id="owner",
            default_language="en",
            default_genre="pop",
            use_plan="publish-everywhere",
        )


def test_music_studio_template_api_preview_list_and_install(application_container):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        templates = client.get("/api/v1/company-templates")
        assert templates.status_code == 200
        assert [value["slug"] for value in templates.json()] == [
            "music-studio",
            "market-intelligence-studio",
        ]

        preview = client.get("/api/v1/company-templates/music-studio/preview")
        assert preview.status_code == 200
        assert preview.json()["installable"]
        assert preview.json()["required_credentials"] == []

        installed = client.post(
            "/api/v1/company-templates/music-studio/install",
            json={
                "company_name": "API Music Studio",
                "default_language": "zh-CN",
                "default_genre": "city pop",
                "use_plan": "internal-demo",
                "operating_timezone": "Asia/Shanghai",
            },
        )
        assert installed.status_code == 201
        payload = installed.json()
        assert payload["company"]["name"] == "API Music Studio"
        assert payload["installation"]["configuration"]["default_genre"] == "city pop"
        assert len(payload["installation"]["resource_refs"]) == 19
