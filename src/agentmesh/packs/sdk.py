from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agentmesh.domain.company_packs import CompanyPack, PackKind
from agentmesh.domain.errors import InvalidCompanyPack

ManifestFactory = Callable[[], dict[str, Any]]
ConfigurationFactory = Callable[[dict[str, Any]], dict[str, Any]]
PACK_SDK_API_VERSION = "0.1"


@dataclass(frozen=True)
class CompanyTemplateDefinition:
    """Declarative contract between the AgentMesh runtime and a business scenario."""

    slug: str
    key: str
    version: str
    name: str
    mission: str
    manifest_factory: ManifestFactory
    configuration_factory: ConfigurationFactory
    required_features: tuple[str, ...]
    sdk_api_version: str = PACK_SDK_API_VERSION
    dependencies: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ("company:manage",)
    external_writes_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.slug.strip():
            raise InvalidCompanyPack("Template slug is required")
        if not self.mission.strip():
            raise InvalidCompanyPack("Template mission is required")
        if self.sdk_api_version != PACK_SDK_API_VERSION:
            raise InvalidCompanyPack(
                f"Unsupported Pack SDK API version '{self.sdk_api_version}'"
            )
        # CompanyPack owns canonical key, version, manifest, and feature validation.
        self.build_pack()

    def manifest(self) -> dict[str, Any]:
        value = deepcopy(self.manifest_factory())
        template = value.get("template")
        if not isinstance(template, dict) or template.get("slug") != self.slug:
            raise InvalidCompanyPack("Template manifest slug must match its definition")
        return value

    def build_pack(self) -> CompanyPack:
        return CompanyPack.create(
            key=self.key,
            version=self.version,
            name=self.name,
            kind=PackKind.TEMPLATE,
            manifest=self.manifest(),
            required_features=list(self.required_features),
            dependencies=list(self.dependencies),
        )

    def normalize_configuration(self, values: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(self.configuration_factory(deepcopy(values)))


class PackCatalog:
    """In-process discovery boundary; a remote registry can implement this API later."""

    def __init__(self, definitions: Iterable[CompanyTemplateDefinition] = ()) -> None:
        self._by_slug: dict[str, CompanyTemplateDefinition] = {}
        self._by_key: dict[str, CompanyTemplateDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: CompanyTemplateDefinition) -> None:
        if definition.slug in self._by_slug:
            raise InvalidCompanyPack(
                f"Template slug '{definition.slug}' is already registered"
            )
        if definition.key in self._by_key:
            raise InvalidCompanyPack(
                f"Template Pack key '{definition.key}' is already registered"
            )
        self._by_slug[definition.slug] = definition
        self._by_key[definition.key] = definition

    def get(self, slug: str) -> CompanyTemplateDefinition:
        value = self._by_slug.get(slug.strip())
        if value is None:
            raise InvalidCompanyPack(f"Unknown Company Template '{slug}'")
        return value

    def list(self) -> list[CompanyTemplateDefinition]:
        return sorted(self._by_slug.values(), key=lambda value: value.slug)
