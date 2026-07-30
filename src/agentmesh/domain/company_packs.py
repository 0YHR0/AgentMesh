from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidCompanyPack
from agentmesh.domain.tasks import utc_now

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class PackKind(str, Enum):
    DOMAIN = "DOMAIN"
    TEMPLATE = "TEMPLATE"


class PackStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


@dataclass
class CompanyPack:
    id: UUID
    key: str
    version: str
    name: str
    kind: PackKind
    manifest: dict[str, Any]
    required_features: list[str]
    dependencies: list[str]
    content_digest: str
    status: PackStatus
    created_at: datetime
    published_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        key: str,
        version: str,
        name: str,
        kind: PackKind,
        manifest: dict[str, Any],
        required_features: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> CompanyPack:
        normalized_key = key.strip().lower()
        if not KEY_PATTERN.fullmatch(normalized_key):
            raise InvalidCompanyPack("Pack key must be a lowercase namespaced key")
        normalized_version = version.strip()
        if not VERSION_PATTERN.fullmatch(normalized_version):
            raise InvalidCompanyPack("Pack version must use MAJOR.MINOR.PATCH")
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 160:
            raise InvalidCompanyPack("Pack name is required and limited to 160 characters")
        document = json.loads(json.dumps(manifest))
        resources = document.get("resources")
        if not isinstance(resources, list) or not resources:
            raise InvalidCompanyPack("Pack manifest requires a non-empty resources list")
        if len(resources) > 200:
            raise InvalidCompanyPack("Pack manifest cannot contain more than 200 resources")
        allowed_kinds = {
            "organization_unit",
            "position",
            "business_object_type",
            "budget_allocation",
            "operating_cycle",
            "objective",
            "key_result",
            "initiative",
            "memory_policy",
            "company_operation",
        }
        seen: set[tuple[str, str]] = set()
        for resource in resources:
            if not isinstance(resource, dict):
                raise InvalidCompanyPack("Pack resources must be objects")
            resource_kind = str(resource.get("kind", "")).strip()
            resource_key = str(resource.get("key", "")).strip().lower()
            if resource_kind not in allowed_kinds:
                raise InvalidCompanyPack(f"Unsupported Pack resource kind '{resource_kind}'")
            if not KEY_PATTERN.fullmatch(resource_key):
                raise InvalidCompanyPack("Pack resource key is invalid")
            identity = (resource_kind, resource_key)
            if identity in seen:
                raise InvalidCompanyPack("Pack resource identities must be unique")
            seen.add(identity)
            if resource_kind == "organization_unit" and not all(
                str(resource.get(field, "")).strip() for field in ("name", "purpose")
            ):
                raise InvalidCompanyPack("Organization Unit resources require name and purpose")
            if resource_kind == "position" and (
                not str(resource.get("unit_key", "")).strip()
                or not str(resource.get("title", "")).strip()
                or not isinstance(resource.get("responsibility_contract"), dict)
                or not resource["responsibility_contract"]
            ):
                raise InvalidCompanyPack(
                    "Position resources require unit_key, title, and responsibility_contract"
                )
            if resource_kind == "business_object_type" and (
                not str(resource.get("name", "")).strip()
                or not isinstance(resource.get("json_schema"), dict)
                or not isinstance(resource.get("lifecycle_definition"), dict)
            ):
                raise InvalidCompanyPack(
                    "Business Object Type resources require name, schema, and lifecycle"
                )
            required_fields = {
                "budget_allocation": ("scope_type", "scope_id"),
                "operating_cycle": ("name",),
                "objective": (
                    "cycle_key",
                    "owner_position_key",
                    "statement",
                    "rationale",
                ),
                "key_result": (
                    "objective_key",
                    "metric_key",
                    "unit",
                    "measurement_source",
                ),
                "initiative": (
                    "objective_key",
                    "owner_unit_key",
                    "title",
                ),
                "memory_policy": (
                    "readable_namespace_patterns",
                    "writable_namespace_patterns",
                    "allowed_memory_types",
                ),
                "company_operation": (
                    "unit_key",
                    "name",
                    "objective_template",
                    "trigger_kind",
                    "missed_policy",
                ),
            }.get(resource_kind, ())
            if any(
                field not in resource or resource[field] is None or resource[field] == ""
                for field in required_fields
            ):
                raise InvalidCompanyPack(f"{resource_kind} resource is missing required fields")
        features = sorted(set(required_features or []))
        deps = sorted(set(dependencies or []))
        canonical = {
            "key": normalized_key,
            "version": normalized_version,
            "kind": kind.value,
            "manifest": document,
            "required_features": features,
            "dependencies": deps,
        }
        return cls(
            id=uuid4(),
            key=normalized_key,
            version=normalized_version,
            name=normalized_name,
            kind=kind,
            manifest=document,
            required_features=features,
            dependencies=deps,
            content_digest=sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            status=PackStatus.DRAFT,
            created_at=utc_now(),
            published_at=None,
        )

    def publish(self) -> None:
        if self.status is not PackStatus.DRAFT:
            raise InvalidCompanyPack("Only a draft Pack can be published")
        self.status = PackStatus.PUBLISHED
        self.published_at = utc_now()


@dataclass(frozen=True)
class PackInstallation:
    id: UUID
    company_id: UUID
    pack_id: UUID
    pack_key: str
    pack_version: str
    pack_digest: str
    installed_by: str
    configuration: dict[str, Any]
    resource_refs: list[dict[str, str]]
    installed_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        pack: CompanyPack,
        installed_by: str,
        resource_refs: list[dict[str, str]],
        configuration: dict[str, Any] | None = None,
    ) -> PackInstallation:
        actor = installed_by.strip()
        if not actor:
            raise InvalidCompanyPack("Pack installer is required")
        return cls(
            id=uuid4(),
            company_id=company_id,
            pack_id=pack.id,
            pack_key=pack.key,
            pack_version=pack.version,
            pack_digest=pack.content_digest,
            installed_by=actor,
            configuration=json.loads(json.dumps(configuration or {})),
            resource_refs=resource_refs,
            installed_at=utc_now(),
        )
