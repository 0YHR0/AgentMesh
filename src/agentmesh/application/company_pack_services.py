from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.business_objects import BusinessObjectType
from agentmesh.domain.company import CompanyStatus, OrganizationUnit, Position
from agentmesh.domain.company_packs import (
    CompanyPack,
    PackInstallation,
    PackStatus,
)
from agentmesh.domain.errors import (
    CompanyPackConflict,
    CompanyPackNotFound,
    InvalidCompanyPack,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class PackPreview:
    pack_id: UUID
    content_digest: str
    required_features: list[str]
    missing_features: list[str]
    missing_dependencies: list[str]
    resources: list[dict[str, str]]
    installable: bool


class CompanyPackService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates

    def create_pack(self, **values: Any) -> CompanyPack:
        self._require_enabled()
        value = CompanyPack.create(**values)
        with self._uow_factory() as uow:
            if uow.company_packs.get_pack_by_key_version(value.key, value.version):
                raise CompanyPackConflict("Pack key and version already exist")
            uow.company_packs.add_pack(value)
            uow.commit()
        return value

    def publish_pack(self, pack_id: UUID) -> CompanyPack:
        self._require_enabled()
        with self._uow_factory() as uow:
            value = self._pack(uow, pack_id)
            for raw in value.required_features:
                try:
                    Feature(raw)
                except ValueError as exc:
                    raise InvalidCompanyPack(
                        f"Pack requires unknown feature '{raw}'"
                    ) from exc
            value.publish()
            uow.company_packs.save_pack(value)
            uow.commit()
            return value

    def list_packs(self) -> list[CompanyPack]:
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.company_packs.list_packs()

    def preview(self, company_id: UUID, pack_id: UUID) -> PackPreview:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            pack = self._pack(uow, pack_id)
            installed = {
                value.pack_key
                for value in uow.company_packs.list_installations(company_id)
            }
            missing_features = [
                raw
                for raw in pack.required_features
                if not self._feature_gates.is_enabled(Feature(raw))
            ]
            missing_dependencies = [
                value for value in pack.dependencies if value not in installed
            ]
            resources = [
                {"kind": value["kind"], "key": value["key"]}
                for value in pack.manifest["resources"]
            ]
            return PackPreview(
                pack_id=pack.id,
                content_digest=pack.content_digest,
                required_features=pack.required_features,
                missing_features=missing_features,
                missing_dependencies=missing_dependencies,
                resources=resources,
                installable=(
                    pack.status is PackStatus.PUBLISHED
                    and not missing_features
                    and not missing_dependencies
                    and uow.company_packs.get_installation(company_id, pack.key) is None
                ),
            )

    def install(
        self,
        company_id: UUID,
        pack_id: UUID,
        *,
        expected_digest: str,
        installed_by: str,
    ) -> PackInstallation:
        self._require_enabled()
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            if company.status is not CompanyStatus.ACTIVE:
                raise CompanyPackConflict("Archived Company cannot install Packs")
            pack = self._pack(uow, pack_id)
            if pack.status is not PackStatus.PUBLISHED:
                raise CompanyPackConflict("Only a published Pack can be installed")
            if expected_digest != pack.content_digest:
                raise CompanyPackConflict("Pack preview digest is stale")
            existing = uow.company_packs.get_installation(company_id, pack.key)
            if existing:
                if existing.pack_digest == pack.content_digest:
                    return existing
                raise CompanyPackConflict(
                    "Pack is already installed; explicit upgrade is required"
                )
            installed = {
                value.pack_key
                for value in uow.company_packs.list_installations(company_id)
            }
            missing_dependencies = set(pack.dependencies) - installed
            if missing_dependencies:
                raise CompanyPackConflict(
                    "Missing Pack dependencies: "
                    + ", ".join(sorted(missing_dependencies))
                )
            for raw in pack.required_features:
                self._feature_gates.require(Feature(raw))
            refs = self._apply_resources(uow, company_id, pack)
            installation = PackInstallation.create(
                company_id=company_id,
                pack=pack,
                installed_by=installed_by,
                resource_refs=refs,
            )
            uow.company_packs.add_installation(installation)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.company.pack.installed",
                    tenant_id=self._tenant_id,
                    aggregate_id=installation.id,
                    payload={
                        "company_id": str(company_id),
                        "pack_key": pack.key,
                        "pack_version": pack.version,
                        "pack_digest": pack.content_digest,
                        "resource_count": len(refs),
                    },
                )
            )
            uow.commit()
            return installation

    def list_installations(self, company_id: UUID) -> list[PackInstallation]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.company_packs.list_installations(company_id)

    def _apply_resources(
        self, uow: Any, company_id: UUID, pack: CompanyPack
    ) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        units: dict[str, OrganizationUnit] = {}
        positions: dict[str, Position] = {}
        resources = pack.manifest["resources"]
        for item in resources:
            if item["kind"] != "organization_unit":
                continue
            if uow.company_model.get_unit_by_key(company_id, item["key"]):
                raise CompanyPackConflict(
                    f"Organization Unit '{item['key']}' already exists"
                )
            parent_key = item.get("parent_key")
            parent = units.get(parent_key) if parent_key else None
            if parent_key and parent is None:
                raise InvalidCompanyPack(
                    f"Organization Unit parent '{parent_key}' must appear first"
                )
            value = OrganizationUnit.create(
                company_id=company_id,
                key=item["key"],
                name=item["name"],
                kind=item.get("unit_kind", "department"),
                purpose=item["purpose"],
                parent_unit_id=parent.id if parent else None,
                memory_namespace=item.get("memory_namespace"),
            )
            uow.company_model.add_unit(value)
            units[value.key] = value
            refs.append({"kind": item["kind"], "key": value.key, "id": str(value.id)})
        for item in resources:
            if item["kind"] != "position":
                continue
            if uow.company_model.get_position_by_key(company_id, item["key"]):
                raise CompanyPackConflict(f"Position '{item['key']}' already exists")
            unit = units.get(item.get("unit_key"))
            if unit is None:
                raise InvalidCompanyPack(
                    f"Position '{item['key']}' references an unknown Pack unit"
                )
            manager = positions.get(item.get("reports_to_key"))
            if item.get("reports_to_key") and manager is None:
                raise InvalidCompanyPack(
                    f"Position manager '{item['reports_to_key']}' must appear first"
                )
            value = Position.create(
                company_id=company_id,
                primary_unit_id=unit.id,
                key=item["key"],
                title=item["title"],
                responsibility_contract=item["responsibility_contract"],
                required_capabilities=item.get("required_capabilities", []),
                allowed_tool_capabilities=item.get("allowed_tool_capabilities", []),
                approval_scope=item.get("approval_scope", {}),
                budget_scope=item.get("budget_scope", {}),
                reports_to_position_id=manager.id if manager else None,
            )
            uow.company_model.add_position(value)
            positions[value.key] = value
            refs.append({"kind": item["kind"], "key": value.key, "id": str(value.id)})
        for item in resources:
            if item["kind"] != "business_object_type":
                continue
            if uow.business_objects.get_type_by_key(
                company_id, item["key"], schema_version=item.get("schema_version", 1)
            ):
                raise CompanyPackConflict(
                    f"Business Object Type '{item['key']}' already exists"
                )
            value = BusinessObjectType.create(
                company_id=company_id,
                key=item["key"],
                name=item["name"],
                schema_version=item.get("schema_version", 1),
                json_schema=item["json_schema"],
                lifecycle_definition=item["lifecycle_definition"],
                sensitive_fields=item.get("sensitive_fields", []),
                ownership_rules=item.get("ownership_rules", {}),
                retention_policy=item.get("retention_policy", {}),
            )
            value.publish()
            uow.business_objects.add_type(value)
            refs.append({"kind": item["kind"], "key": value.key, "id": str(value.id)})
        return refs

    def _company(self, uow: Any, company_id: UUID):
        value = uow.company_model.get_company(company_id)
        if value is None or value.tenant_id != self._tenant_id:
            raise CompanyPackNotFound(f"Company {company_id} was not found")
        return value

    @staticmethod
    def _pack(uow: Any, pack_id: UUID) -> CompanyPack:
        value = uow.company_packs.get_pack(pack_id)
        if value is None:
            raise CompanyPackNotFound(f"Company Pack {pack_id} was not found")
        return value

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.COMPANY_PACKS)
