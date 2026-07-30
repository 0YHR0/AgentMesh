import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.domain.company_packs import PackKind, PackStatus
from agentmesh.domain.errors import CompanyPackConflict
from agentmesh.features import FeatureGateSet
from agentmesh.templates.market_intelligence_studio import build_pack, manifest


def _manifest():
    return {
        "resources": [
            {
                "kind": "organization_unit",
                "key": "research",
                "name": "Research",
                "purpose": "Produce evidence-backed market research.",
            },
            {
                "kind": "position",
                "key": "research-lead",
                "unit_key": "research",
                "title": "Research Lead",
                "responsibility_contract": {
                    "outcome": "Verified research plans and evidence."
                },
                "required_capabilities": ["research"],
            },
            {
                "kind": "business_object_type",
                "key": "research-brief",
                "name": "Research Brief",
                "json_schema": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
                "lifecycle_definition": {
                    "states": ["DRAFT", "READY"],
                    "initial_state": "DRAFT",
                    "actions": {
                        "submit": {
                            "from": ["DRAFT"],
                            "to": "READY",
                            "allowed_update_fields": [],
                        }
                    },
                },
            },
        ]
    }


def test_pack_preview_digest_pinning_and_atomic_resource_install(
    company_pack_service,
    company_service,
    uow_factory,
):
    company = company_service.create_company(
        name="Pack Company",
        mission="Install declarative company semantics safely.",
        owner_principal_id="owner",
    )
    pack = company_pack_service.create_pack(
        key="studio.core",
        version="1.0.0",
        name="Studio Core",
        kind=PackKind.TEMPLATE,
        manifest=_manifest(),
        required_features=["company_model", "business_objects"],
    )
    assert pack.status is PackStatus.DRAFT
    pack = company_pack_service.publish_pack(pack.id)
    preview = company_pack_service.preview(company.id, pack.id)
    assert preview.installable
    assert len(preview.resources) == 3

    with pytest.raises(CompanyPackConflict, match="stale"):
        company_pack_service.install(
            company.id,
            pack.id,
            expected_digest="0" * 64,
            installed_by="owner",
        )
    installed = company_pack_service.install(
        company.id,
        pack.id,
        expected_digest=preview.content_digest,
        installed_by="owner",
    )
    repeated = company_pack_service.install(
        company.id,
        pack.id,
        expected_digest=preview.content_digest,
        installed_by="owner",
    )
    assert repeated.id == installed.id
    assert len(installed.resource_refs) == 3
    with uow_factory() as uow:
        assert uow.company_model.get_unit_by_key(company.id, "research")
        assert uow.company_model.get_position_by_key(company.id, "research-lead")
        object_type = uow.business_objects.get_type_by_key(
            company.id, "research-brief", published_only=True
        )
        assert object_type is not None


def test_pack_dependency_is_visible_before_install(
    company_pack_service,
    company_service,
):
    company = company_service.create_company(
        name="Dependent Pack Company",
        mission="Refuse incomplete declarative compositions.",
        owner_principal_id="owner",
    )
    pack = company_pack_service.create_pack(
        key="studio.extension",
        version="1.0.0",
        name="Studio Extension",
        kind=PackKind.DOMAIN,
        manifest={
            "resources": [
                {
                    "kind": "organization_unit",
                    "key": "sales",
                    "name": "Sales",
                    "purpose": "Manage governed commercial proposals.",
                }
            ]
        },
        dependencies=["studio.core"],
    )
    company_pack_service.publish_pack(pack.id)
    preview = company_pack_service.preview(company.id, pack.id)
    assert preview.missing_dependencies == ["studio.core"]
    assert not preview.installable


def test_pack_api_previews_and_installs(
    application_container,
    company_service,
):
    company = company_service.create_company(
        name="Pack API Company",
        mission="Install a previewed template through the control plane.",
        owner_principal_id="owner",
    )
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            "/api/v1/company-packs",
            json={
                "key": "api.studio",
                "version": "1.0.0",
                "name": "API Studio",
                "kind": "TEMPLATE",
                "manifest": _manifest(),
                "required_features": ["company_model", "business_objects"],
            },
        )
        assert created.status_code == 201
        pack_id = created.json()["id"]
        assert client.post(f"/api/v1/company-packs/{pack_id}/publish").status_code == 200
        preview = client.get(
            f"/api/v1/company-packs/{pack_id}/companies/{company.id}/preview"
        )
        assert preview.status_code == 200
        installed = client.post(
            f"/api/v1/company-packs/{pack_id}/companies/{company.id}/install",
            json={"expected_digest": preview.json()["content_digest"]},
        )
        assert installed.status_code == 200
        assert len(installed.json()["resource_refs"]) == 3


def test_market_intelligence_template_is_stable_and_complete():
    first = build_pack()
    second = build_pack()
    resources = manifest()["resources"]

    assert first.content_digest == second.content_digest
    assert first.key == "agentmesh.market-intelligence-studio"
    assert sum(item["kind"] == "organization_unit" for item in resources) == 8
    assert sum(item["kind"] == "position" for item in resources) == 17
    assert sum(item["kind"] == "business_object_type" for item in resources) == 7


def test_market_intelligence_template_provisions_company_atomically(
    company_pack_service,
    uow_factory,
):
    preview = company_pack_service.preview_market_intelligence_template()
    assert preview.installable
    assert preview.required_credentials == []
    assert preview.permissions == ["company:manage"]
    assert not preview.external_writes_enabled
    assert preview.resource_summary == {
        "organization_unit": 8,
        "position": 17,
        "business_object_type": 7,
    }

    result = company_pack_service.install_market_intelligence_template(
        company_name="APAC Intelligence Studio",
        owner_principal_id="owner",
        target_market="APAC developer infrastructure buyers",
        product_type="subscription",
        excluded_sectors=["weapons", "gambling"],
        operating_timezone="Asia/Shanghai",
    )

    assert result.company.name == "APAC Intelligence Studio"
    assert result.installation.pack_digest == preview.content_digest
    assert result.installation.configuration == {
        "target_market": "APAC developer infrastructure buyers",
        "product_type": "subscription",
        "excluded_sectors": ["gambling", "weapons"],
    }
    assert len(result.installation.resource_refs) == 32
    with uow_factory() as uow:
        assert len(uow.company_model.list_units(result.company.id)) == 8
        assert len(uow.company_model.list_positions(result.company.id)) == 17
        assert len(uow.company_packs.list_installations(result.company.id)) == 1
        assert (
            uow.business_objects.get_type_by_key(
                result.company.id, "research-report", published_only=True
            )
            is not None
        )

    with pytest.raises(CompanyPackConflict, match="active Company"):
        company_pack_service.install_market_intelligence_template(
            company_name="Duplicate Studio",
            owner_principal_id="owner",
            target_market="A second market",
            product_type="research-report",
        )


def test_market_intelligence_template_api_preview_and_install(
    application_container,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        preview = client.get(
            "/api/v1/company-templates/market-intelligence-studio/preview"
        )
        assert preview.status_code == 200
        assert preview.json()["installable"]
        assert preview.json()["required_credentials"] == []

        installed = client.post(
            "/api/v1/company-templates/market-intelligence-studio/install",
            json={
                "company_name": "API Intelligence Studio",
                "target_market": "Independent software vendors",
                "product_type": "custom-research",
                "excluded_sectors": ["weapons"],
                "operating_timezone": "Asia/Shanghai",
            },
        )
        assert installed.status_code == 201
        payload = installed.json()
        assert payload["company"]["name"] == "API Intelligence Studio"
        assert payload["installation"]["configuration"]["product_type"] == (
            "custom-research"
        )
        assert len(payload["installation"]["resource_refs"]) == 32
