from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.domain.company_packs import PackKind, PackStatus
from agentmesh.domain.errors import CompanyPackConflict, InvalidCompanyOperation
from agentmesh.domain.identity import PrincipalContext, PrincipalType
from agentmesh.domain.mcp_registry import McpTransport
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentVersion,
    AgentVisibility,
)
from agentmesh.domain.tasks import TaskExecutionMode, TaskStatus
from agentmesh.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSideEffect
from agentmesh.features import FeatureGateSet
from agentmesh.templates.market_intelligence_operations import (
    build_pack as build_operations_pack,
)
from agentmesh.templates.market_intelligence_operations import (
    manifest as operations_manifest,
)
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
                "responsibility_contract": {"outcome": "Verified research plans and evidence."},
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


def _published_role_agent(
    uow_factory,
    *,
    name: str,
    capabilities: list[str],
    allowed_tools: list[str] | None = None,
):
    definition = AgentDefinition.create(
        tenant_id="test-tenant",
        owner_id="owner",
        name=name,
        description=f"Qualified employee for {name}.",
        visibility=AgentVisibility.TENANT,
        tags=["market-intelligence", "employee"],
    )
    version = AgentVersion.create_draft(
        definition_id=definition.id,
        semantic_version="1.0.0",
        role=name,
        instructions="Complete the appointed Position responsibility with evidence.",
        declared_capabilities=capabilities,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tool_profile=(
            {"allowed_tools": allowed_tools, "max_calls": 6}
            if allowed_tools
            else None
        ),
        runtime_adapter="deterministic-local",
        execution_modes=["async"],
    )
    version.submit_for_review()
    version.publish(capabilities)
    definition.set_default(version)
    with uow_factory() as uow:
        uow.agent_definitions.add(definition)
        uow.agent_versions.add(version)
        uow.commit()
    return definition, version


def _publish_research_tools(registry_service):
    server = registry_service.register_server(
        owner_id="owner",
        name="research-source-provider",
        description="Read-only search and source retrieval for governed research.",
        transport=McpTransport.MANAGED_STDIO,
        endpoint_reference="managed://research-source-provider",
        actor="owner",
        idempotency_key="research-source-server",
    )
    version = registry_service.add_version(
        server.id,
        semantic_version="1.0.0",
        protocol_version="2025-11-25",
        configuration={"adapter": "test-research-source"},
        actor="owner",
        idempotency_key="research-source-version",
    )
    for logical_key, tool_name in (
        ("web.search", "search"),
        ("source.read", "read"),
    ):
        registry_service.add_tool(
            version.id,
            logical_key=logical_key,
            tool_name=tool_name,
            description=f"Read-only {logical_key} capability.",
            side_effect=ToolSideEffect.READ_ONLY,
            input_schema={"type": "object", "additionalProperties": True},
            actor="owner",
            idempotency_key=f"research-tool-{tool_name}",
        )
    registry_service.publish_version(
        version.id,
        principal=PrincipalContext(
            principal_id="owner",
            tenant_id="test-tenant",
            principal_type=PrincipalType.USER,
            roles=frozenset(),
            authenticated=True,
            authentication_method="test",
        ),
        permit_id=None,
    )


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


def test_pack_upgrade_validates_existing_objects_and_is_idempotent(
    company_pack_service,
    company_service,
    business_object_service,
    uow_factory,
):
    company = company_service.create_company(
        name="Upgradeable Pack Company",
        mission="Upgrade installed capabilities without rebuilding the company.",
        owner_principal_id="owner",
    )
    old_pack = company_pack_service.create_pack(
        key="studio.upgradeable",
        version="1.0.0",
        name="Upgradeable Studio",
        kind=PackKind.TEMPLATE,
        manifest=_manifest(),
        required_features=["company_model", "business_objects"],
    )
    company_pack_service.publish_pack(old_pack.id)
    installation = company_pack_service.install(
        company.id,
        old_pack.id,
        expected_digest=old_pack.content_digest,
        installed_by="owner",
    )
    with uow_factory() as uow:
        object_type = uow.business_objects.get_type_by_key(
            company.id, "research-brief", published_only=True
        )
        assert object_type is not None
        original_type_id = object_type.id
    created = business_object_service.create_object(
        company.id,
        type_id=original_type_id,
        data={"question": "Which market should we enter?"},
        actor="owner",
    )

    target_manifest = _manifest()
    target_type = next(
        value
        for value in target_manifest["resources"]
        if value["kind"] == "business_object_type"
    )
    target_type["schema_version"] = 2
    target_type["json_schema"]["properties"]["decision"] = {"type": "string"}
    target_pack = company_pack_service.create_pack(
        key="studio.upgradeable",
        version="1.1.0",
        name="Upgradeable Studio",
        kind=PackKind.TEMPLATE,
        manifest=target_manifest,
        required_features=["company_model", "business_objects"],
    )
    company_pack_service.publish_pack(target_pack.id)

    preview = company_pack_service.preview_upgrade(company.id, target_pack.id)
    assert preview.upgradeable
    assert preview.affected_object_count == 1
    assert preview.resource_changes == [
        {
            "kind": "business_object_type",
            "key": "research-brief",
            "change": "updated",
            "from_schema_version": 1,
            "to_schema_version": 2,
            "affected_object_count": 1,
        }
    ]
    result = company_pack_service.upgrade(
        company.id,
        target_pack.id,
        expected_from_digest=installation.pack_digest,
        expected_target_digest=target_pack.content_digest,
        upgraded_by="owner",
    )
    replay = company_pack_service.upgrade(
        company.id,
        target_pack.id,
        expected_from_digest=installation.pack_digest,
        expected_target_digest=target_pack.content_digest,
        upgraded_by="owner",
    )

    assert result.installation.id == installation.id
    assert result.installation.revision == 2
    assert result.upgrade.migrated_object_count == 1
    assert replay.upgrade.id == result.upgrade.id
    assert len(company_pack_service.list_upgrades(company.id)) == 1
    with uow_factory() as uow:
        upgraded_type = uow.business_objects.get_type_by_key(
            company.id, "research-brief", published_only=True
        )
        assert upgraded_type is not None
        assert upgraded_type.id == original_type_id
        assert upgraded_type.schema_version == 2
        stored_object = uow.business_objects.get_object(created.object.id)
        assert stored_object is not None
        assert stored_object.type_id == original_type_id


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
        preview = client.get(f"/api/v1/company-packs/{pack_id}/companies/{company.id}/preview")
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


def test_market_intelligence_operations_pack_is_stable_and_safe():
    first = build_operations_pack()
    second = build_operations_pack()
    resources = operations_manifest()["resources"]

    assert first.content_digest == second.content_digest
    assert first.dependencies == ["agentmesh.market-intelligence-studio"]
    assert sum(item["kind"] == "key_result" for item in resources) == 4
    assert sum(item["kind"] == "company_operation" for item in resources) == 3
    assert operations_manifest()["operations"]["safety"] == {
        "operations_start_in_draft": True,
        "external_writes_enabled": False,
    }


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


def test_market_intelligence_operations_activate_governed_company_runtime(
    company_pack_service,
    uow_factory,
):
    installed = company_pack_service.install_market_intelligence_template(
        company_name="Operating Studio",
        owner_principal_id="owner",
        target_market="Infrastructure engineering leaders",
        product_type="research-report",
        operating_timezone="Asia/Shanghai",
    )
    preview = company_pack_service.preview_market_intelligence_operations()
    assert preview.installable
    assert preview.base_pack_installed
    assert not preview.external_writes_enabled
    assert preview.operations_start_in_draft
    assert preview.resource_summary == {
        "budget_allocation": 1,
        "operating_cycle": 1,
        "objective": 1,
        "key_result": 4,
        "initiative": 1,
        "memory_policy": 1,
        "company_operation": 3,
    }

    activation = company_pack_service.activate_market_intelligence_operations(
        installed_by="owner",
        starts_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        cycle_days=28,
        budget_limit_micros=25_000_000,
        currency="USD",
    )
    assert len(activation.resource_refs) == 12
    assert activation.configuration["budget_limit_micros"] == 25_000_000
    company_id = installed.company.id
    with uow_factory() as uow:
        cycle = uow.company_goals.get_active_cycle(company_id)
        assert cycle is not None
        objectives = uow.company_goals.list_objectives(cycle.id)
        assert len(objectives) == 1
        assert objectives[0].status.value == "ACTIVE"
        assert len(uow.company_goals.list_key_results(objectives[0].id)) == 4
        assert len(uow.company_goals.list_initiatives(objectives[0].id)) == 1
        operations = uow.company_operations.list_operations(company_id)
        assert len(operations) == 3
        assert {value.status.value for value in operations} == {"DRAFT"}
        assert {value.timezone for value in operations} == {"Asia/Shanghai"}
        assert len(uow.organizational_memory.list_policies(company_id)) == 1
        allocations = uow.financial_governance.list_allocations(company_id)
        assert len(allocations) == 1
        assert allocations[0].approved_limit_micros == 25_000_000

    repeated = company_pack_service.activate_market_intelligence_operations(
        installed_by="owner",
        starts_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert repeated.id == activation.id


def test_market_intelligence_workforce_appoints_and_runs_coordinated_operations(
    company_pack_service,
    company_operation_service,
    uow_factory,
):
    installed = company_pack_service.install_market_intelligence_template(
        company_name="Staffed Studio",
        owner_principal_id="owner",
        target_market="Infrastructure leaders",
        product_type="research-report",
        operating_timezone="UTC",
    )
    company_pack_service.activate_market_intelligence_operations(
        installed_by="owner",
        starts_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    empty = company_pack_service.preview_market_intelligence_workforce()
    assert len(empty.positions) == 6
    assert not empty.fully_staffed
    assert empty.activatable_operation_count == 0

    assignments = []
    for position in empty.positions:
        _definition, version = _published_role_agent(
            uow_factory,
            name=f"employee-{position['key']}",
            capabilities=position["required_capabilities"],
        )
        assignments.append(
            {
                "position_key": position["key"],
                "agent_version_id": str(version.id),
            }
        )
    appointments = company_pack_service.appoint_market_intelligence_workforce(
        assignments=assignments,
        appointed_by="owner",
        reason="Staff the first governed operating cycle.",
    )
    assert len(appointments) == 6
    staffed = company_pack_service.preview_market_intelligence_workforce()
    assert staffed.fully_staffed
    assert staffed.activatable_operation_count == 3
    assert all(value["ready"] for value in staffed.operations)

    activated_at = datetime(2026, 8, 3, tzinfo=timezone.utc)
    operations = company_operation_service.activate_staffed_operations(
        installed.company.id,
        operation_keys=[],
        activated_at=activated_at,
    )
    assert len(operations) == 3
    launches = company_operation_service.dispatch_due(
        now=activated_at.replace(day=10)
    )
    assert len(launches) == 3
    assert all(value.task is not None for value in launches)
    for launch in launches:
        assert launch.task is not None
        assert launch.task.task.execution_mode is TaskExecutionMode.COORDINATED
        assert len(launch.task.subtasks) == 2
        assert all(value.preferred_agent_id for value in launch.task.subtasks)
        workforce = launch.task.task.input["company_context"]["workforce"]
        assert len(workforce) == 2
        assert all(value["appointment_id"] for value in workforce)


def test_market_intelligence_operation_activation_fails_closed_without_appointments(
    company_pack_service,
    company_operation_service,
):
    installed = company_pack_service.install_market_intelligence_template(
        company_name="Unstaffed Studio",
        owner_principal_id="owner",
        target_market="Infrastructure leaders",
        product_type="research-report",
    )
    company_pack_service.activate_market_intelligence_operations(
        installed_by="owner",
        starts_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    with pytest.raises(
        InvalidCompanyOperation,
        match="staffing preflight failed.*has no active Appointment",
    ):
        company_operation_service.activate_staffed_operations(
            installed.company.id,
            operation_keys=[],
            activated_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )

    assert {
        value.status.value
        for value in company_operation_service.list_operations(installed.company.id)
    } == {"DRAFT"}


def test_market_intelligence_template_api_preview_and_install(
    application_container,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        preview = client.get("/api/v1/company-templates/market-intelligence-studio/preview")
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
        assert payload["installation"]["configuration"]["product_type"] == ("custom-research")
        assert len(payload["installation"]["resource_refs"]) == 32


def test_market_intelligence_operations_api_preview_and_activate(
    application_container,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        (
            "company_model=true,company_goals=true,company_operations=true,"
            "business_objects=true,organizational_memory=true,"
            "company_finance_read=true,financial_governance=true,"
            "company_packs=true"
        ),
    )
    with TestClient(create_app(application_container)) as client:
        installed = client.post(
            "/api/v1/company-templates/market-intelligence-studio/install",
            json={
                "company_name": "Operating API Studio",
                "target_market": "Technology strategy teams",
                "product_type": "subscription",
                "operating_timezone": "Asia/Shanghai",
            },
        )
        assert installed.status_code == 201
        preview = client.get(
            "/api/v1/company-templates/market-intelligence-studio/operations/preview"
        )
        assert preview.status_code == 200
        assert preview.json()["installable"]

        activation = client.post(
            "/api/v1/company-templates/market-intelligence-studio/operations/activate",
            json={
                "starts_at": "2026-08-03T00:00:00Z",
                "cycle_days": 28,
                "budget_limit_micros": 30_000_000,
                "currency": "USD",
            },
        )
        assert activation.status_code == 201
        assert activation.json()["pack_key"] == ("agentmesh.market-intelligence-operations")
        assert len(activation.json()["resource_refs"]) == 12


def test_market_intelligence_workforce_api_appoints_and_starts_operations(
    application_container,
    uow_factory,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        (
            "company_model=true,company_goals=true,company_operations=true,"
            "business_objects=true,organizational_memory=true,"
            "company_finance_read=true,financial_governance=true,"
            "company_packs=true"
        ),
    )
    with TestClient(create_app(application_container)) as client:
        company = client.post(
            "/api/v1/company-templates/market-intelligence-studio/install",
            json={
                "company_name": "API Workforce Studio",
                "target_market": "Research buyers",
                "product_type": "research-report",
            },
        ).json()["company"]
        assert (
            client.post(
                "/api/v1/company-templates/market-intelligence-studio/operations/activate",
                json={"starts_at": "2026-08-03T00:00:00Z"},
            ).status_code
            == 201
        )
        preview = client.get(
            "/api/v1/company-templates/market-intelligence-studio/workforce/preview"
        )
        assert preview.status_code == 200
        assignments = []
        for position in preview.json()["positions"]:
            _definition, version = _published_role_agent(
                uow_factory,
                name=f"api-{position['key']}",
                capabilities=position["required_capabilities"],
            )
            assignments.append(
                {
                    "position_key": position["key"],
                    "agent_version_id": str(version.id),
                }
            )
        appointed = client.post(
            "/api/v1/company-templates/market-intelligence-studio/workforce/appoint",
            json={
                "assignments": assignments,
                "reason": "API staffing preflight passed.",
            },
        )
        assert appointed.status_code == 201
        assert len(appointed.json()["appointments"]) == 6

        ready = client.get(
            "/api/v1/company-templates/market-intelligence-studio/workforce/preview"
        ).json()
        assert ready["fully_staffed"]
        activated = client.post(
            f"/api/v1/companies/{company['id']}/operations/_activate/staffed",
            json={
                "operation_keys": [],
                "activated_at": "2026-08-03T00:00:00Z",
            },
        )
        assert activated.status_code == 200
        assert len(activated.json()) == 3
        assert {value["status"] for value in activated.json()} == {"ACTIVE"}


def test_live_market_research_preflight_fails_closed_then_launches_governed_task(
    application_container,
    uow_factory,
):
    service = application_container.company_pack_service
    installed = service.install_market_intelligence_template(
        company_name="Live Research Studio",
        owner_principal_id="owner",
        target_market="Enterprise infrastructure buyers",
        product_type="research-report",
    )

    missing = application_container.market_research_service.preflight()
    assert not missing.ready
    assert {value["subject"] for value in missing.blockers}.issuperset(
        {"web.search", "source.read", "research-lead", "research-specialist"}
    )

    _publish_research_tools(application_container.mcp_registry_service)
    assignments = []
    for position_key in (
        "research-lead",
        "research-specialist",
        "fact-reviewer",
        "editorial-reviewer",
    ):
        with uow_factory() as uow:
            position = uow.company_model.get_position_by_key(
                installed.company.id, position_key
            )
        assert position is not None
        _definition, version = _published_role_agent(
            uow_factory,
            name=f"live-{position_key}",
            capabilities=list(position.required_capabilities),
            allowed_tools=(
                ["web.search", "source.read"]
                if position_key in {"research-lead", "research-specialist"}
                else None
            ),
        )
        assignments.append(
            {"position_key": position_key, "agent_version_id": str(version.id)}
        )
    service.appoint_market_intelligence_workforce(
        assignments=assignments,
        appointed_by="owner",
        reason="Staff the live research engagement.",
    )

    ready = application_container.market_research_service.preflight()
    assert ready.ready, ready.blockers
    assert not ready.external_writes_enabled
    assert all(value["ready"] for value in ready.positions)
    assert {value["logical_key"] for value in ready.tools} == {
        "web.search",
        "source.read",
    }

    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        (
            "company_model=true,company_packs=true,business_objects=true,"
            "identity_rbac=true,policy_approval=true,governed_mcp=true"
        ),
    )
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/company-templates/market-intelligence-studio/research/launch",
            headers={"Idempotency-Key": "first-live-study"},
            json={
                "question": "Which governance controls matter most to enterprise AI buyers?",
                "target_audience": "Product and strategy leaders",
                "decision_supported": "Prioritize the next AgentMesh product release",
                "scope": "Public evidence published during the last twelve months.",
                "max_sources": 12,
            },
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["preflight"]["ready"]
    assert payload["task"]["status"] == "RUNNING"
    assert payload["task"]["execution_mode"] == "COORDINATED"
    subtasks = {value["key"]: value for value in payload["task"]["subtasks"]}
    assert set(subtasks) == {
        "scope-plan",
        "evidence-collection",
        "claim-synthesis",
        "fact-check",
        "report-draft",
    }
    assert subtasks["report-draft"]["depends_on"] == ["fact-check"]
    assert payload["research_question"]["object"]["lifecycle_state"] == "DRAFT"
    assert payload["task"]["input"]["evidence_contract"]["report"].startswith(
        "Internal draft only"
    )
    replay = application_container.market_research_service.launch(
        question="Which governance controls matter most to enterprise AI buyers?",
        target_audience="Product and strategy leaders",
        decision_supported="Prioritize the next AgentMesh product release",
        scope="Public evidence published during the last twelve months.",
        max_sources=12,
        requested_by="owner",
        idempotency_key="first-live-study",
    )
    assert str(replay.task.task.id) == payload["task"]["id"]
    assert str(replay.research_question.object.id) == payload["research_question"]["object"]["id"]

    task_id = UUID(payload["task"]["id"])
    invocation_id = uuid4()
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        task.status = TaskStatus.COMPLETED
        uow.tasks.save(task)
        report_subtask = next(
            value
            for value in uow.subtasks.list_for_task(task_id, for_update=True)
            if value.key == "report-draft"
        )
        report_subtask.output = {
            "summary": "A governed research report is ready for review.",
            "research_deliverable": {
                "sources": [
                    {
                        "title": "Enterprise AI governance survey",
                        "uri": "https://example.com/governance-survey",
                        "publisher": "Example Research",
                        "retrieved_at": "2026-07-31T08:00:00Z",
                        "excerpt_digest": f"sha256:{'a' * 64}",
                        "tool_invocation_ids": [str(invocation_id)],
                    }
                ],
                "claims": [
                    {
                        "claim": "Enterprise buyers prioritize auditable approvals.",
                        "source_uris": ["https://example.com/governance-survey"],
                        "confidence": "HIGH",
                        "limitations": "The public survey covers only large enterprises.",
                    }
                ],
                "report": {
                    "title": "Enterprise AI Governance Priorities",
                    "audience": "Product and strategy leaders",
                    "markdown": "# Findings\n\nAuditable approvals are a leading requirement.",
                },
            },
        }
        uow.subtasks.save(report_subtask)
        now = datetime.now(timezone.utc)
        uow.tool_invocations.add(
            ToolInvocation(
                id=invocation_id,
                tenant_id="test-tenant",
                task_id=task_id,
                run_id=uuid4(),
                server_name="research-source-provider",
                tool_key="web.search",
                tool_name="search",
                side_effect=ToolSideEffect.READ_ONLY,
                protocol_version="2025-11-25",
                schema_digest=f"sha256:{'b' * 64}",
                arguments_digest=f"sha256:{'c' * 64}",
                status=ToolInvocationStatus.SUCCEEDED,
                result_digest=f"sha256:{'d' * 64}",
                result_bytes=128,
                error=None,
                started_at=now,
                completed_at=now,
            )
        )
        uow.commit()

    with TestClient(create_app(application_container)) as client:
        status_response = client.get(
            f"/api/v1/company-templates/market-intelligence-studio/research/tasks/"
            f"{task_id}/materialization"
        )
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "READY"
        materialized = client.post(
            f"/api/v1/company-templates/market-intelligence-studio/research/tasks/"
            f"{task_id}/materialize"
        )
        assert materialized.status_code == 200, materialized.text
        result = materialized.json()
        assert result["status"] == "MATERIALIZED"
        assert len(result["source_record_ids"]) == 1
        assert len(result["claim_register_ids"]) == 1
        assert result["report_id"]
        assert result["artifact_id"]

    replayed = application_container.research_materialization_service.materialize(
        task_id, actor="owner"
    )
    assert replayed.status == "MATERIALIZED"
    assert replayed.report_id == UUID(result["report_id"])
    assert replayed.source_record_ids == [UUID(result["source_record_ids"][0])]
    assert replayed.claim_register_ids == [UUID(result["claim_register_ids"][0])]
    assert len(uow_factory.store.artifacts) == 1
