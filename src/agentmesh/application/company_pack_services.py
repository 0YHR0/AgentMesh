from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.business_objects import BusinessObjectType
from agentmesh.domain.company import (
    Appointment,
    Company,
    CompanyStatus,
    OrganizationUnit,
    Position,
)
from agentmesh.domain.company_goals import (
    CompanyObjective,
    Initiative,
    KeyResult,
    OperatingCycle,
)
from agentmesh.domain.company_operations import (
    CompanyOperation,
    MissedSchedulePolicy,
    TriggerKind,
)
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
from agentmesh.domain.financial_governance import AllocationScope, BudgetAllocation
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.organizational_memory import (
    MemoryPolicy,
    MemorySensitivity,
    MemoryType,
)
from agentmesh.domain.registry import AgentDefinitionLifecycle, AgentVersionStatus
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.templates.market_intelligence_operations import (
    DEFAULT_BUDGET_LIMIT_MICROS,
    DEFAULT_CYCLE_DAYS,
)
from agentmesh.templates.market_intelligence_operations import (
    PACK_KEY as OPERATIONS_PACK_KEY,
)
from agentmesh.templates.market_intelligence_operations import (
    build_pack as build_operations_pack,
)
from agentmesh.templates.market_intelligence_studio import (
    DEFAULT_MISSION,
    PRODUCT_TYPES,
    TEMPLATE_SLUG,
    build_pack,
)
from agentmesh.templates.music_studio import (
    DEFAULT_MISSION as MUSIC_STUDIO_DEFAULT_MISSION,
)
from agentmesh.templates.music_studio import TEMPLATE_SLUG as MUSIC_STUDIO_TEMPLATE_SLUG
from agentmesh.templates.music_studio import USE_PLANS as MUSIC_STUDIO_USE_PLANS
from agentmesh.templates.music_studio import build_pack as build_music_studio_pack


@dataclass(frozen=True)
class PackPreview:
    pack_id: UUID
    content_digest: str
    required_features: list[str]
    missing_features: list[str]
    missing_dependencies: list[str]
    resources: list[dict[str, str]]
    installable: bool


@dataclass(frozen=True)
class CompanyTemplatePreview:
    slug: str
    name: str
    version: str
    mission: str
    content_digest: str
    required_features: list[str]
    missing_features: list[str]
    resource_summary: dict[str, int]
    resources: list[dict[str, str]]
    required_credentials: list[str]
    permissions: list[str]
    external_writes_enabled: bool
    active_company_id: UUID | None
    installable: bool


@dataclass(frozen=True)
class CompanyTemplateInstallation:
    company: Company
    installation: PackInstallation


@dataclass(frozen=True)
class CompanyOperationsPreview:
    name: str
    version: str
    content_digest: str
    active_company_id: UUID | None
    base_pack_installed: bool
    already_installed: bool
    required_features: list[str]
    missing_features: list[str]
    resource_summary: dict[str, int]
    resources: list[dict[str, str]]
    operations_start_in_draft: bool
    external_writes_enabled: bool
    installable: bool


@dataclass(frozen=True)
class CompanyWorkforcePreview:
    active_company_id: UUID | None
    operations_pack_installed: bool
    missing_features: list[str]
    positions: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    fully_staffed: bool
    activatable_operation_count: int


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
                    raise InvalidCompanyPack(f"Pack requires unknown feature '{raw}'") from exc
            value.publish()
            uow.company_packs.save_pack(value)
            uow.commit()
            return value

    def list_packs(self) -> list[CompanyPack]:
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.company_packs.list_packs()

    def preview_music_studio_template(self) -> CompanyTemplatePreview:
        self._require_enabled()
        pack = build_music_studio_pack()
        missing_features = [
            raw
            for raw in pack.required_features
            if not self._feature_gates.is_enabled(Feature(raw))
        ]
        summary: dict[str, int] = {}
        resources: list[dict[str, str]] = []
        for item in pack.manifest["resources"]:
            summary[item["kind"]] = summary.get(item["kind"], 0) + 1
            resources.append(
                {
                    "kind": item["kind"],
                    "key": item["key"],
                    "name": item.get("name", item.get("title", item["key"])),
                }
            )
        with self._uow_factory() as uow:
            active = uow.company_model.get_active_company(self._tenant_id)
        return CompanyTemplatePreview(
            slug=MUSIC_STUDIO_TEMPLATE_SLUG,
            name=pack.name,
            version=pack.version,
            mission=MUSIC_STUDIO_DEFAULT_MISSION,
            content_digest=pack.content_digest,
            required_features=pack.required_features,
            missing_features=missing_features,
            resource_summary=summary,
            resources=resources,
            required_credentials=[],
            permissions=["company:manage"],
            external_writes_enabled=False,
            active_company_id=active.id if active else None,
            installable=not missing_features and active is None,
        )

    def install_music_studio_template(
        self,
        *,
        company_name: str,
        owner_principal_id: str,
        default_language: str,
        default_genre: str,
        use_plan: str,
        mission: str = MUSIC_STUDIO_DEFAULT_MISSION,
        default_currency: str = "USD",
        operating_timezone: str = "UTC",
    ) -> CompanyTemplateInstallation:
        self._require_enabled()
        language = default_language.strip()
        if not 2 <= len(language) <= 32:
            raise InvalidCompanyPack("Default language must contain 2 to 32 characters")
        genre = default_genre.strip()
        if not 1 <= len(genre) <= 120:
            raise InvalidCompanyPack("Default genre must contain 1 to 120 characters")
        normalized_use_plan = use_plan.strip().lower()
        if normalized_use_plan not in MUSIC_STUDIO_USE_PLANS:
            raise InvalidCompanyPack(
                "Use plan must be one of: " + ", ".join(MUSIC_STUDIO_USE_PLANS)
            )
        candidate = build_music_studio_pack()
        for raw in candidate.required_features:
            self._feature_gates.require(Feature(raw))
        configuration = {
            "default_language": language,
            "default_genre": genre,
            "use_plan": normalized_use_plan,
            "generation_provider": "deterministic-demo",
            "external_writes_enabled": False,
        }
        company = Company.create(
            tenant_id=self._tenant_id,
            name=company_name,
            mission=mission,
            owner_principal_id=owner_principal_id,
            default_currency=default_currency,
            operating_timezone=operating_timezone,
        )
        with self._uow_factory() as uow:
            uow.idempotency.lock(f"active-company:{self._tenant_id}", self._tenant_id)
            if uow.company_model.get_active_company(self._tenant_id) is not None:
                raise CompanyPackConflict("This tenant already has an active Company")
            pack = self._resolve_builtin_pack(uow, candidate)
            uow.company_model.add_company(company)
            refs = self._apply_resources(
                uow,
                company.id,
                pack,
                configuration=configuration,
                installed_by=owner_principal_id,
            )
            installation = PackInstallation.create(
                company_id=company.id,
                pack=pack,
                installed_by=owner_principal_id,
                resource_refs=refs,
                configuration=configuration,
            )
            uow.company_packs.add_installation(installation)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.company.created",
                    tenant_id=self._tenant_id,
                    aggregate_id=company.id,
                    payload={"name": company.name, "template": MUSIC_STUDIO_TEMPLATE_SLUG},
                )
            )
            uow.outbox.add(self._installation_event(installation))
            uow.commit()
        return CompanyTemplateInstallation(company=company, installation=installation)

    def preview_market_intelligence_template(self) -> CompanyTemplatePreview:
        self._require_enabled()
        pack = build_pack()
        missing_features = [
            raw
            for raw in pack.required_features
            if not self._feature_gates.is_enabled(Feature(raw))
        ]
        summary: dict[str, int] = {}
        resources: list[dict[str, str]] = []
        for item in pack.manifest["resources"]:
            summary[item["kind"]] = summary.get(item["kind"], 0) + 1
            resources.append(
                {
                    "kind": item["kind"],
                    "key": item["key"],
                    "name": item.get("name", item.get("title", item["key"])),
                }
            )
        with self._uow_factory() as uow:
            active = uow.company_model.get_active_company(self._tenant_id)
        return CompanyTemplatePreview(
            slug=TEMPLATE_SLUG,
            name=pack.name,
            version=pack.version,
            mission=DEFAULT_MISSION,
            content_digest=pack.content_digest,
            required_features=pack.required_features,
            missing_features=missing_features,
            resource_summary=summary,
            resources=resources,
            required_credentials=[],
            permissions=["company:manage"],
            external_writes_enabled=False,
            active_company_id=active.id if active else None,
            installable=not missing_features and active is None,
        )

    def install_market_intelligence_template(
        self,
        *,
        company_name: str,
        owner_principal_id: str,
        target_market: str,
        product_type: str,
        excluded_sectors: list[str] | None = None,
        mission: str = DEFAULT_MISSION,
        default_currency: str = "USD",
        operating_timezone: str = "UTC",
    ) -> CompanyTemplateInstallation:
        self._require_enabled()
        target = target_market.strip()
        if not target or len(target) > 500:
            raise InvalidCompanyPack("Target market is required and limited to 500 characters")
        normalized_product = product_type.strip().lower()
        if normalized_product not in PRODUCT_TYPES:
            raise InvalidCompanyPack("Product type must be one of: " + ", ".join(PRODUCT_TYPES))
        excluded = sorted({value.strip() for value in (excluded_sectors or []) if value.strip()})
        if len(excluded) > 20 or any(len(value) > 160 for value in excluded):
            raise InvalidCompanyPack("Excluded sectors are limited to 20 values of 160 characters")
        candidate = build_pack()
        for raw in candidate.required_features:
            self._feature_gates.require(Feature(raw))
        configuration = {
            "target_market": target,
            "product_type": normalized_product,
            "excluded_sectors": excluded,
        }
        company = Company.create(
            tenant_id=self._tenant_id,
            name=company_name,
            mission=mission,
            owner_principal_id=owner_principal_id,
            default_currency=default_currency,
            operating_timezone=operating_timezone,
        )
        with self._uow_factory() as uow:
            uow.idempotency.lock(f"active-company:{self._tenant_id}", self._tenant_id)
            if uow.company_model.get_active_company(self._tenant_id) is not None:
                raise CompanyPackConflict("This tenant already has an active Company")
            pack = uow.company_packs.get_pack_by_key_version(candidate.key, candidate.version)
            if pack is None:
                candidate.publish()
                uow.company_packs.add_pack(candidate)
                pack = candidate
            elif pack.content_digest != candidate.content_digest:
                raise CompanyPackConflict("Built-in template key is occupied by different content")
            elif pack.status is PackStatus.DRAFT:
                pack.publish()
                uow.company_packs.save_pack(pack)
            elif pack.status is not PackStatus.PUBLISHED:
                raise CompanyPackConflict("Built-in template is not installable")
            uow.company_model.add_company(company)
            refs = self._apply_resources(
                uow,
                company.id,
                pack,
                configuration=configuration,
                installed_by=owner_principal_id,
            )
            installation = PackInstallation.create(
                company_id=company.id,
                pack=pack,
                installed_by=owner_principal_id,
                resource_refs=refs,
                configuration=configuration,
            )
            uow.company_packs.add_installation(installation)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.company.created",
                    tenant_id=self._tenant_id,
                    aggregate_id=company.id,
                    payload={"name": company.name, "template": TEMPLATE_SLUG},
                )
            )
            uow.outbox.add(self._installation_event(installation))
            uow.commit()
        return CompanyTemplateInstallation(
            company=company,
            installation=installation,
        )

    def preview_market_intelligence_operations(self) -> CompanyOperationsPreview:
        self._require_enabled()
        pack = build_operations_pack()
        missing_features = [
            raw
            for raw in pack.required_features
            if not self._feature_gates.is_enabled(Feature(raw))
        ]
        summary: dict[str, int] = {}
        resources: list[dict[str, str]] = []
        for item in pack.manifest["resources"]:
            summary[item["kind"]] = summary.get(item["kind"], 0) + 1
            resources.append(
                {
                    "kind": item["kind"],
                    "key": item["key"],
                    "name": item.get(
                        "name",
                        item.get("title", item.get("statement", item["key"])),
                    ),
                }
            )
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            base_installed = bool(
                company and uow.company_packs.get_installation(company.id, build_pack().key)
            )
            already_installed = bool(
                company and uow.company_packs.get_installation(company.id, OPERATIONS_PACK_KEY)
            )
        return CompanyOperationsPreview(
            name=pack.name,
            version=pack.version,
            content_digest=pack.content_digest,
            active_company_id=company.id if company else None,
            base_pack_installed=base_installed,
            already_installed=already_installed,
            required_features=pack.required_features,
            missing_features=missing_features,
            resource_summary=summary,
            resources=resources,
            operations_start_in_draft=True,
            external_writes_enabled=False,
            installable=bool(
                company and base_installed and not already_installed and not missing_features
            ),
        )

    def activate_market_intelligence_operations(
        self,
        *,
        installed_by: str,
        starts_at: datetime,
        cycle_days: int = DEFAULT_CYCLE_DAYS,
        budget_limit_micros: int = DEFAULT_BUDGET_LIMIT_MICROS,
        currency: str | None = None,
    ) -> PackInstallation:
        self._require_enabled()
        if starts_at.tzinfo is None:
            raise InvalidCompanyPack("Operations start timestamp must be timezone-aware")
        if not 7 <= cycle_days <= 365:
            raise InvalidCompanyPack("Operating Cycle duration must be 7 to 365 days")
        if budget_limit_micros < 1:
            raise InvalidCompanyPack("Initial budget limit must be positive")
        candidate = build_operations_pack()
        for raw in candidate.required_features:
            self._feature_gates.require(Feature(raw))
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                raise CompanyPackConflict("Install the Market Intelligence Studio template first")
            uow.idempotency.lock(f"pack-install:{company.id}:{candidate.key}", self._tenant_id)
            if not uow.company_packs.get_installation(company.id, build_pack().key):
                raise CompanyPackConflict(
                    "Market Intelligence Operations requires its base template"
                )
            existing = uow.company_packs.get_installation(company.id, candidate.key)
            if existing:
                if existing.pack_digest == candidate.content_digest:
                    return existing
                raise CompanyPackConflict(
                    "Operations Pack is already installed with different content"
                )
            pack = self._resolve_builtin_pack(uow, candidate)
            configuration = {
                "starts_at": starts_at.astimezone(timezone.utc).isoformat(),
                "cycle_days": cycle_days,
                "budget_limit_micros": budget_limit_micros,
                "currency": (currency or company.default_currency).upper(),
                "operating_timezone": company.operating_timezone,
            }
            refs = self._apply_resources(
                uow,
                company.id,
                pack,
                configuration=configuration,
                installed_by=installed_by,
            )
            installation = PackInstallation.create(
                company_id=company.id,
                pack=pack,
                installed_by=installed_by,
                resource_refs=refs,
                configuration=configuration,
            )
            uow.company_packs.add_installation(installation)
            uow.outbox.add(self._installation_event(installation))
            uow.commit()
            return installation

    def preview_market_intelligence_workforce(self) -> CompanyWorkforcePreview:
        self._require_enabled()
        missing_features = [
            feature.value
            for feature in (
                Feature.AGENT_REGISTRY_MANAGEMENT,
                Feature.COORDINATED_EXECUTION,
                Feature.COMPANY_OPERATIONS,
            )
            if not self._feature_gates.is_enabled(feature)
        ]
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                return CompanyWorkforcePreview(
                    active_company_id=None,
                    operations_pack_installed=False,
                    missing_features=missing_features,
                    positions=[],
                    operations=[],
                    fully_staffed=False,
                    activatable_operation_count=0,
                )
            operations_installed = bool(
                uow.company_packs.get_installation(company.id, OPERATIONS_PACK_KEY)
            )
            operations = (
                uow.company_operations.list_operations(company.id)
                if operations_installed
                else []
            )
            required_position_ids = {
                position_id
                for operation in operations
                for position_id in operation.position_bindings
            }
            definitions = {
                value.id: value
                for value in uow.agent_definitions.list(
                    tenant_id=self._tenant_id, limit=1_000, offset=0
                )
                if value.lifecycle is AgentDefinitionLifecycle.ACTIVE
            }
            default_versions = {}
            for definition in definitions.values():
                if definition.default_version_id is None:
                    continue
                version = uow.agent_versions.get(definition.default_version_id)
                if version and version.status is AgentVersionStatus.PUBLISHED:
                    default_versions[definition.id] = version
            position_rows: list[dict[str, Any]] = []
            readiness: dict[UUID, bool] = {}
            position_keys: dict[UUID, str] = {}
            for position_id in sorted(required_position_ids, key=str):
                position = uow.company_model.get_position(position_id)
                if position is None or position.company_id != company.id:
                    continue
                appointment = uow.company_model.get_active_appointment(position.id)
                appointed_definition = (
                    definitions.get(appointment.agent_definition_id)
                    if appointment
                    else None
                )
                appointed_version = (
                    uow.agent_versions.get(appointment.agent_version_id)
                    if appointment
                    else None
                )
                ready = bool(
                    appointment
                    and appointed_definition
                    and appointed_version
                    and appointed_definition.default_version_id
                    == appointed_version.id
                    and appointed_version.status is AgentVersionStatus.PUBLISHED
                    and appointed_version.content_digest
                    and "async" in appointed_version.execution_modes
                    and set(position.required_capabilities).issubset(
                        appointed_version.verified_capabilities
                    )
                )
                readiness[position.id] = ready
                position_keys[position.id] = position.key
                candidates = [
                    {
                        "agent_definition_id": str(definition.id),
                        "agent_name": definition.name,
                        "agent_version_id": str(version.id),
                        "semantic_version": version.semantic_version,
                        "verified_capabilities": list(
                            version.verified_capabilities
                        ),
                    }
                    for definition in definitions.values()
                    if (version := default_versions.get(definition.id))
                    and version.content_digest
                    and "async" in version.execution_modes
                    and set(position.required_capabilities).issubset(
                        version.verified_capabilities
                    )
                ]
                candidates.sort(
                    key=lambda value: (
                        value["agent_name"],
                        value["semantic_version"],
                    )
                )
                position_rows.append(
                    {
                        "position_id": str(position.id),
                        "key": position.key,
                        "title": position.title,
                        "required_capabilities": list(
                            position.required_capabilities
                        ),
                        "appointment_id": (
                            str(appointment.id) if appointment else None
                        ),
                        "appointed_agent_name": (
                            appointed_definition.name
                            if appointed_definition
                            else None
                        ),
                        "appointed_agent_version_id": (
                            str(appointed_version.id)
                            if appointed_version
                            else None
                        ),
                        "ready": ready,
                        "candidates": candidates,
                    }
                )
            position_rows.sort(key=lambda value: value["key"])
            operation_rows: list[dict[str, Any]] = []
            for operation in operations:
                blockers = [
                    position_keys.get(position_id, str(position_id))
                    for position_id in operation.position_bindings
                    if not readiness.get(position_id, False)
                ]
                operation_rows.append(
                    {
                        "operation_id": str(operation.id),
                        "key": operation.key,
                        "name": operation.name,
                        "status": operation.status.value,
                        "position_keys": [
                            position_keys.get(position_id, str(position_id))
                            for position_id in operation.position_bindings
                        ],
                        "blockers": blockers,
                        "ready": not blockers,
                    }
                )
            operation_rows.sort(key=lambda value: value["key"])
            return CompanyWorkforcePreview(
                active_company_id=company.id,
                operations_pack_installed=operations_installed,
                missing_features=missing_features,
                positions=position_rows,
                operations=operation_rows,
                fully_staffed=bool(position_rows)
                and all(value["ready"] for value in position_rows),
                activatable_operation_count=sum(
                    value["ready"]
                    and value["status"] in {"DRAFT", "PAUSED"}
                    for value in operation_rows
                )
                if not missing_features
                else 0,
            )

    def appoint_market_intelligence_workforce(
        self,
        *,
        assignments: list[dict[str, Any]],
        appointed_by: str,
        reason: str,
    ) -> list[Appointment]:
        self._require_enabled()
        self._feature_gates.require(Feature.AGENT_REGISTRY_MANAGEMENT)
        if not 1 <= len(assignments) <= 17:
            raise InvalidCompanyPack("Workforce assignment count must be 1 to 17")
        position_keys = [str(value.get("position_key", "")).strip() for value in assignments]
        if len(set(position_keys)) != len(position_keys) or any(
            not value for value in position_keys
        ):
            raise InvalidCompanyPack(
                "Workforce assignments require unique Position keys"
            )
        allowed = {
            value["key"]
            for value in build_pack().manifest["resources"]
            if value["kind"] == "position"
        }
        if not set(position_keys).issubset(allowed):
            raise InvalidCompanyPack(
                "Workforce assignments must target template Positions"
            )
        created: list[Appointment] = []
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                raise CompanyPackConflict(
                    "Install the Market Intelligence Studio template first"
                )
            uow.idempotency.lock(
                f"workforce-appointment:{company.id}", self._tenant_id
            )
            for assignment in assignments:
                position_key = str(assignment["position_key"]).strip()
                position = uow.company_model.get_position_by_key(
                    company.id, position_key
                )
                if position is None:
                    raise CompanyPackConflict(
                        f"Template Position '{position_key}' was not found"
                    )
                try:
                    version_id = UUID(str(assignment["agent_version_id"]))
                except (KeyError, ValueError) as exc:
                    raise InvalidCompanyPack(
                        f"Position '{position_key}' requires an Agent Version ID"
                    ) from exc
                version = uow.agent_versions.get(version_id)
                definition = (
                    uow.agent_definitions.get(version.definition_id)
                    if version
                    else None
                )
                if (
                    version is None
                    or version.status is not AgentVersionStatus.PUBLISHED
                    or definition is None
                    or definition.tenant_id != self._tenant_id
                    or definition.lifecycle
                    is not AgentDefinitionLifecycle.ACTIVE
                    or definition.default_version_id != version.id
                    or not version.content_digest
                    or "async" not in version.execution_modes
                ):
                    raise InvalidCompanyPack(
                        f"Position '{position_key}' requires an active Agent's "
                        "published, async-capable default Version"
                    )
                missing = set(position.required_capabilities) - set(
                    version.verified_capabilities
                )
                if missing:
                    raise InvalidCompanyPack(
                        f"Agent Version for '{position_key}' lacks: "
                        + ", ".join(sorted(missing))
                    )
                existing = uow.company_model.get_active_appointment(position.id)
                if existing:
                    if existing.agent_version_id == version.id:
                        created.append(existing)
                        continue
                    raise CompanyPackConflict(
                        f"Position '{position_key}' already has an active Appointment"
                    )
                appointment = Appointment.create(
                    company_id=company.id,
                    position_id=position.id,
                    agent_definition_id=definition.id,
                    agent_version_id=version.id,
                    appointed_by=appointed_by,
                    reason=reason,
                )
                uow.company_model.add_appointment(appointment)
                uow.outbox.add(
                    MessageEnvelope.domain_event(
                        schema_name="agentmesh.company.appointment.started",
                        tenant_id=self._tenant_id,
                        aggregate_id=appointment.id,
                        payload={
                            "company_id": str(company.id),
                            "position_id": str(position.id),
                            "agent_definition_id": str(definition.id),
                            "agent_version_id": str(version.id),
                            "source": "market-intelligence-workforce",
                        },
                    )
                )
                created.append(appointment)
            uow.commit()
            return created

    def preview(self, company_id: UUID, pack_id: UUID) -> PackPreview:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            pack = self._pack(uow, pack_id)
            installed = {
                value.pack_key for value in uow.company_packs.list_installations(company_id)
            }
            missing_features = [
                raw
                for raw in pack.required_features
                if not self._feature_gates.is_enabled(Feature(raw))
            ]
            missing_dependencies = [value for value in pack.dependencies if value not in installed]
            resources = [
                {"kind": value["kind"], "key": value["key"]} for value in pack.manifest["resources"]
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
        configuration: dict[str, Any] | None = None,
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
                raise CompanyPackConflict("Pack is already installed; explicit upgrade is required")
            installed = {
                value.pack_key for value in uow.company_packs.list_installations(company_id)
            }
            missing_dependencies = set(pack.dependencies) - installed
            if missing_dependencies:
                raise CompanyPackConflict(
                    "Missing Pack dependencies: " + ", ".join(sorted(missing_dependencies))
                )
            for raw in pack.required_features:
                self._feature_gates.require(Feature(raw))
            refs = self._apply_resources(
                uow,
                company_id,
                pack,
                configuration=configuration,
                installed_by=installed_by,
            )
            installation = PackInstallation.create(
                company_id=company_id,
                pack=pack,
                installed_by=installed_by,
                resource_refs=refs,
                configuration=configuration,
            )
            uow.company_packs.add_installation(installation)
            uow.outbox.add(self._installation_event(installation))
            uow.commit()
            return installation

    def list_installations(self, company_id: UUID) -> list[PackInstallation]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.company_packs.list_installations(company_id)

    def _apply_resources(
        self,
        uow: Any,
        company_id: UUID,
        pack: CompanyPack,
        *,
        configuration: dict[str, Any] | None = None,
        installed_by: str = "system",
    ) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        units: dict[str, OrganizationUnit] = {}
        positions: dict[str, Position] = {}
        allocations: dict[str, BudgetAllocation] = {}
        cycles: dict[str, OperatingCycle] = {}
        objectives: dict[str, CompanyObjective] = {}
        initiatives: dict[str, Initiative] = {}
        config = dict(configuration or {})
        resources = pack.manifest["resources"]
        for item in resources:
            if item["kind"] != "organization_unit":
                continue
            if uow.company_model.get_unit_by_key(company_id, item["key"]):
                raise CompanyPackConflict(f"Organization Unit '{item['key']}' already exists")
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
                raise CompanyPackConflict(f"Business Object Type '{item['key']}' already exists")
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
        for item in resources:
            if item["kind"] != "budget_allocation":
                continue
            limit = int(
                config.get(
                    "budget_limit_micros",
                    item.get("default_limit_micros", DEFAULT_BUDGET_LIMIT_MICROS),
                )
            )
            value = BudgetAllocation.create(
                company_id=company_id,
                scope_type=AllocationScope(item["scope_type"]),
                scope_id=item["scope_id"],
                currency=str(config.get("currency", "USD")),
                approved_limit_micros=limit,
                policy_version=int(item.get("policy_version", 1)),
                parent_allocation_id=(
                    allocations[item["parent_key"]].id if item.get("parent_key") else None
                ),
            )
            uow.financial_governance.add_allocation(value)
            allocations[item["key"]] = value
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        for item in resources:
            if item["kind"] != "operating_cycle":
                continue
            if uow.company_goals.get_active_cycle(company_id):
                raise CompanyPackConflict("Company already has an active Operating Cycle")
            raw_start = config.get("starts_at")
            if isinstance(raw_start, str):
                starts_at = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            elif isinstance(raw_start, datetime):
                starts_at = raw_start
            else:
                starts_at = datetime.now(timezone.utc)
            duration = int(config.get("cycle_days", item.get("duration_days", 28)))
            value = OperatingCycle.create(
                company_id=company_id,
                name=item["name"],
                starts_at=starts_at,
                ends_at=starts_at + timedelta(days=duration),
                review_schedule=item.get("review_schedule", {}),
            )
            if item.get("activate", False):
                value.approve(installed_by)
                value.activate()
            uow.company_goals.add_cycle(value)
            cycles[item["key"]] = value
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        if cycles:
            uow.flush()
        for item in resources:
            if item["kind"] != "objective":
                continue
            cycle = cycles.get(item["cycle_key"])
            owner = positions.get(item["owner_position_key"]) or (
                uow.company_model.get_position_by_key(company_id, item["owner_position_key"])
            )
            if cycle is None or owner is None:
                raise InvalidCompanyPack(f"Objective '{item['key']}' has unresolved references")
            value = CompanyObjective.create(
                company_id=company_id,
                cycle_id=cycle.id,
                owner_position_id=owner.id,
                statement=item["statement"],
                rationale=item["rationale"],
                priority=int(item.get("priority", 3)),
                target_date=cycle.ends_at,
            )
            if item.get("activate", False):
                value.approve()
                value.activate()
            uow.company_goals.add_objective(value)
            objectives[item["key"]] = value
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        if objectives:
            uow.flush()
        for item in resources:
            if item["kind"] != "key_result":
                continue
            objective = objectives.get(item["objective_key"])
            if objective is None:
                raise InvalidCompanyPack(f"Key Result '{item['key']}' has an unknown Objective")
            value = KeyResult.create(
                company_id=company_id,
                objective_id=objective.id,
                metric_key=item["metric_key"],
                unit=item["unit"],
                baseline=str(item.get("baseline", "0")),
                target=str(item.get("target", "1")),
                measurement_source=item["measurement_source"],
            )
            uow.company_goals.add_key_result(value)
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        for item in resources:
            if item["kind"] != "initiative":
                continue
            objective = objectives.get(item["objective_key"])
            unit = units.get(item["owner_unit_key"]) or uow.company_model.get_unit_by_key(
                company_id, item["owner_unit_key"]
            )
            allocation = allocations.get(item.get("budget_allocation_key"))
            if objective is None or unit is None:
                raise InvalidCompanyPack(f"Initiative '{item['key']}' has unresolved references")
            value = Initiative.create(
                company_id=company_id,
                objective_id=objective.id,
                owner_unit_id=unit.id,
                title=item["title"],
                outcome_contract=item["outcome_contract"],
                budget_allocation_id=allocation.id if allocation else None,
            )
            if item.get("activate", False):
                value.approve()
                value.activate()
            uow.company_goals.add_initiative(value)
            initiatives[item["key"]] = value
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        if initiatives:
            uow.flush()
        for item in resources:
            if item["kind"] != "memory_policy":
                continue
            if uow.organizational_memory.get_policy_by_key(company_id, item["key"]):
                raise CompanyPackConflict(f"Memory Policy '{item['key']}' already exists")
            value = MemoryPolicy.create(
                company_id=company_id,
                key=item["key"],
                version=int(item.get("version", 1)),
                readable_namespace_patterns=item["readable_namespace_patterns"],
                writable_namespace_patterns=item["writable_namespace_patterns"],
                allowed_memory_types=[MemoryType(raw) for raw in item["allowed_memory_types"]],
                auto_accept_memory_types=[
                    MemoryType(raw) for raw in item.get("auto_accept_memory_types", [])
                ],
                forbidden_sensitivity_levels=[
                    MemorySensitivity(raw) for raw in item.get("forbidden_sensitivity_levels", [])
                ],
                maximum_retrieval_count=int(item.get("maximum_retrieval_count", 10)),
                maximum_context_tokens=int(item.get("maximum_context_tokens", 2000)),
                default_ttl_seconds=item.get("default_ttl_seconds"),
                review_role=item.get("review_role", "company-owner"),
                extraction_enabled=bool(item.get("extraction_enabled", False)),
            )
            uow.organizational_memory.add_policy(value)
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        for item in resources:
            if item["kind"] != "company_operation":
                continue
            if uow.company_operations.get_operation_by_key(company_id, item["key"]):
                raise CompanyPackConflict(f"Company Operation '{item['key']}' already exists")
            unit = units.get(item["unit_key"]) or uow.company_model.get_unit_by_key(
                company_id, item["unit_key"]
            )
            bindings = [
                positions.get(key) or uow.company_model.get_position_by_key(company_id, key)
                for key in item.get("position_keys", [])
            ]
            if unit is None or any(value is None for value in bindings):
                raise InvalidCompanyPack(
                    f"Company Operation '{item['key']}' has unresolved references"
                )
            initiative = initiatives.get(item.get("initiative_key"))
            value = CompanyOperation.create(
                company_id=company_id,
                organization_unit_id=unit.id,
                initiative_id=initiative.id if initiative else None,
                key=item["key"],
                name=item["name"],
                objective_template=item["objective_template"],
                input_template=item.get("input_template", {}),
                trigger_kind=TriggerKind(item["trigger_kind"]),
                trigger_definition=item.get("trigger_definition"),
                timezone=str(config.get("operating_timezone", "UTC")),
                missed_policy=MissedSchedulePolicy(item["missed_policy"]),
                catch_up_limit=int(item.get("catch_up_limit", 1)),
                concurrency_limit=int(item.get("concurrency_limit", 1)),
                maximum_runs_per_window=int(item.get("maximum_runs_per_window", 4)),
                window_seconds=int(item.get("window_seconds", 604800)),
                position_bindings=[value.id for value in bindings if value],
                tool_capability_allowlist=item.get("tool_capability_allowlist", []),
                budget_limit={
                    "currency": str(config.get("currency", "USD")),
                    "amount_micros": int(
                        config.get(
                            "budget_limit_micros",
                            DEFAULT_BUDGET_LIMIT_MICROS,
                        )
                    ),
                },
            )
            uow.company_operations.add_operation(value)
            refs.append({"kind": item["kind"], "key": item["key"], "id": str(value.id)})
        return refs

    @staticmethod
    def _resolve_builtin_pack(uow: Any, candidate: CompanyPack) -> CompanyPack:
        pack = uow.company_packs.get_pack_by_key_version(candidate.key, candidate.version)
        if pack is None:
            candidate.publish()
            uow.company_packs.add_pack(candidate)
            return candidate
        if pack.content_digest != candidate.content_digest:
            raise CompanyPackConflict("Built-in Pack key is occupied by different content")
        if pack.status is PackStatus.DRAFT:
            pack.publish()
            uow.company_packs.save_pack(pack)
        elif pack.status is not PackStatus.PUBLISHED:
            raise CompanyPackConflict("Built-in Pack is not installable")
        return pack

    def _installation_event(self, installation: PackInstallation) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name="agentmesh.company.pack.installed",
            tenant_id=self._tenant_id,
            aggregate_id=installation.id,
            payload={
                "company_id": str(installation.company_id),
                "pack_key": installation.pack_key,
                "pack_version": installation.pack_version,
                "pack_digest": installation.pack_digest,
                "resource_count": len(installation.resource_refs),
                "configuration": installation.configuration,
            },
        )

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
