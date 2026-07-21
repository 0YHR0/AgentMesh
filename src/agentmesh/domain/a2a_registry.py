from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidA2ARegistry, InvalidA2ATransition
from agentmesh.domain.tasks import utc_now

PEER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"1.0"})
SUPPORTED_BINDINGS = frozenset({"JSONRPC", "HTTP+JSON", "GRPC"})
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class A2APeerStatus(str, Enum):
    REGISTERED = "REGISTERED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class A2ATrustTier(str, Enum):
    RESTRICTED = "RESTRICTED"
    TRUSTED = "TRUSTED"
    HIGH_ASSURANCE = "HIGH_ASSURANCE"


class AgentCardSignatureStatus(str, Enum):
    UNSIGNED = "UNSIGNED"
    PRESENT_UNVERIFIED = "PRESENT_UNVERIFIED"


class AgentCardSource(str, Enum):
    MANUAL = "MANUAL"
    DISCOVERED = "DISCOVERED"


def _https_url(value: str, *, field_name: str) -> tuple[str, str]:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InvalidA2ARegistry(
            f"{field_name} must be an HTTPS URL without credentials or fragment"
        )
    if len(normalized) > 2048:
        raise InvalidA2ARegistry(f"{field_name} is too long")
    return normalized, parsed.hostname.lower().rstrip(".")


def _reject_secret_material(value: Any) -> None:
    sensitive_keys = {
        "accesstoken",
        "apikeyvalue",
        "authorization",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "refreshtoken",
        "secret",
        "token",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in sensitive_keys:
                raise InvalidA2ARegistry(
                    f"Agent Card must not contain credential material in field '{key}'"
                )
            _reject_secret_material(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_material(child)


@dataclass(frozen=True)
class A2APeer:
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    discovery_url: str
    allowed_endpoint_hosts: tuple[str, ...]
    allowed_bindings: tuple[str, ...]
    trust_tier: A2ATrustTier
    status: A2APeerStatus
    active_card_snapshot_id: UUID | None
    created_at: datetime
    updated_at: datetime
    revision: int = 1

    @classmethod
    def register(
        cls,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        discovery_url: str,
        allowed_endpoint_hosts: list[str] | tuple[str, ...],
        allowed_bindings: list[str] | tuple[str, ...],
        trust_tier: A2ATrustTier,
    ) -> A2APeer:
        tenant = tenant_id.strip()
        owner = owner_id.strip()
        normalized_name = name.strip().lower()
        if not tenant or not owner or not PEER_NAME_PATTERN.fullmatch(normalized_name):
            raise InvalidA2ARegistry(
                "Peer tenant/owner are required and name must be 3-63 lowercase characters"
            )
        normalized_url, discovery_host = _https_url(discovery_url, field_name="discovery_url")
        hosts = {value.strip().lower().rstrip(".") for value in allowed_endpoint_hosts}
        hosts.discard("")
        hosts.add(discovery_host)
        if len(hosts) > 32 or any(not HOSTNAME_PATTERN.fullmatch(host) for host in hosts):
            raise InvalidA2ARegistry("allowed_endpoint_hosts must contain at most 32 DNS hostnames")
        bindings = tuple(dict.fromkeys(value.strip().upper() for value in allowed_bindings))
        if not bindings or not set(bindings).issubset(SUPPORTED_BINDINGS):
            raise InvalidA2ARegistry("Peer must allow one or more supported A2A bindings")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=tenant,
            owner_id=owner,
            name=normalized_name,
            discovery_url=normalized_url,
            allowed_endpoint_hosts=tuple(sorted(hosts)),
            allowed_bindings=bindings,
            trust_tier=trust_tier,
            status=A2APeerStatus.REGISTERED,
            active_card_snapshot_id=None,
            created_at=now,
            updated_at=now,
        )

    def select_card(self, snapshot_id: UUID) -> A2APeer:
        return replace(
            self,
            active_card_snapshot_id=snapshot_id,
            status=A2APeerStatus.ACTIVE,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def suspend(self) -> A2APeer:
        if self.status is A2APeerStatus.SUSPENDED:
            return self
        return replace(
            self,
            status=A2APeerStatus.SUSPENDED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def revoke_active_card(self) -> A2APeer:
        if self.active_card_snapshot_id is None:
            raise InvalidA2ATransition("Peer has no active Agent Card to revoke")
        return replace(
            self,
            active_card_snapshot_id=None,
            status=A2APeerStatus.REGISTERED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class A2AEndpoint:
    url: str
    protocol_binding: str
    protocol_version: str
    tenant: str | None


@dataclass(frozen=True)
class A2ASkillCandidate:
    skill_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    input_modes: tuple[str, ...]
    output_modes: tuple[str, ...]


@dataclass(frozen=True)
class AgentCardSnapshot:
    id: UUID
    tenant_id: str
    peer_id: UUID
    digest: str
    raw_card: dict[str, Any]
    agent_name: str
    agent_description: str
    agent_version: str
    endpoints: tuple[A2AEndpoint, ...]
    skills: tuple[A2ASkillCandidate, ...]
    capabilities: dict[str, Any]
    security_schemes: dict[str, Any]
    signature_status: AgentCardSignatureStatus
    fetched_at: datetime
    expires_at: datetime
    source_etag: str | None
    source: AgentCardSource
    source_url: str | None

    @classmethod
    def import_card(
        cls,
        *,
        tenant_id: str,
        peer: A2APeer,
        raw_card: dict[str, Any],
        ttl_seconds: int,
        source_etag: str | None = None,
        source: AgentCardSource = AgentCardSource.MANUAL,
        source_url: str | None = None,
        max_bytes: int = 262_144,
    ) -> AgentCardSnapshot:
        _reject_secret_material(raw_card)
        encoded_card = json.dumps(
            raw_card, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(encoded_card) > max_bytes:
            raise InvalidA2ARegistry(f"Agent Card exceeds the {max_bytes} byte limit")
        if ttl_seconds < 60 or ttl_seconds > 86_400:
            raise InvalidA2ARegistry("Agent Card TTL must be between 60 and 86400 seconds")
        if tenant_id != peer.tenant_id:
            raise InvalidA2ARegistry("Agent Card tenant must match its Peer")
        required = {
            "name",
            "description",
            "supportedInterfaces",
            "version",
            "capabilities",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
        }
        missing = sorted(required - raw_card.keys())
        if missing:
            raise InvalidA2ARegistry(
                f"Agent Card is missing required field(s): {', '.join(missing)}"
            )
        name = raw_card["name"]
        description = raw_card["description"]
        agent_version = raw_card["version"]
        capabilities = raw_card["capabilities"]
        security_schemes = raw_card.get("securitySchemes", {})
        if not all(isinstance(value, str) for value in (name, description, agent_version)):
            raise InvalidA2ARegistry("Agent Card name, description, and version must be strings")
        if not name.strip() or not agent_version.strip() or not isinstance(capabilities, dict):
            raise InvalidA2ARegistry("Agent Card identity and capabilities are invalid")
        if not isinstance(security_schemes, dict):
            raise InvalidA2ARegistry("Agent Card securitySchemes must be an object")
        for modes_name in ("defaultInputModes", "defaultOutputModes"):
            modes = raw_card[modes_name]
            if (
                not isinstance(modes, list)
                or not modes
                or not all(isinstance(x, str) for x in modes)
            ):
                raise InvalidA2ARegistry(
                    f"Agent Card {modes_name} must be a non-empty string array"
                )

        interfaces = raw_card["supportedInterfaces"]
        if not isinstance(interfaces, list) or not interfaces or len(interfaces) > 16:
            raise InvalidA2ARegistry("Agent Card must contain 1-16 supportedInterfaces")
        endpoints: list[A2AEndpoint] = []
        for item in interfaces:
            if not isinstance(item, dict):
                raise InvalidA2ARegistry("Agent Card interface must be an object")
            try:
                url = item["url"]
                binding = item["protocolBinding"]
                version = item["protocolVersion"]
            except KeyError as exc:
                raise InvalidA2ARegistry(
                    "Agent Card interface is missing a required field"
                ) from exc
            if not all(isinstance(value, str) for value in (url, binding, version)):
                raise InvalidA2ARegistry("Agent Card interface fields must be strings")
            normalized_url, host = _https_url(url, field_name="interface url")
            binding = binding.strip().upper()
            version = version.strip()
            if host not in peer.allowed_endpoint_hosts:
                raise InvalidA2ARegistry(
                    f"Agent Card endpoint host '{host}' is not allowed by Peer"
                )
            if binding not in peer.allowed_bindings or binding not in SUPPORTED_BINDINGS:
                raise InvalidA2ARegistry(f"A2A binding '{binding}' is not allowed by Peer")
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise InvalidA2ARegistry(f"A2A protocol version '{version}' is unsupported")
            tenant = item.get("tenant")
            if tenant is not None and (not isinstance(tenant, str) or not tenant.strip()):
                raise InvalidA2ARegistry("Agent Card interface tenant must be a non-empty string")
            endpoints.append(A2AEndpoint(normalized_url, binding, version, tenant))

        raw_skills = raw_card["skills"]
        if not isinstance(raw_skills, list) or not 1 <= len(raw_skills) <= 200:
            raise InvalidA2ARegistry("Agent Card skills must contain 1-200 items")
        skills: list[A2ASkillCandidate] = []
        seen_skills: set[str] = set()
        for item in raw_skills:
            if not isinstance(item, dict):
                raise InvalidA2ARegistry("Agent Card skill must be an object")
            try:
                skill_id, skill_name, skill_description, tags = (
                    item["id"],
                    item["name"],
                    item["description"],
                    item["tags"],
                )
            except KeyError as exc:
                raise InvalidA2ARegistry("Agent Card skill is missing a required field") from exc
            if (
                not all(
                    isinstance(value, str) for value in (skill_id, skill_name, skill_description)
                )
                or not skill_id.strip()
                or not isinstance(tags, list)
                or not tags
                or not all(isinstance(tag, str) for tag in tags)
            ):
                raise InvalidA2ARegistry("Agent Card skill fields are invalid")
            normalized_id = skill_id.strip()
            if normalized_id in seen_skills:
                raise InvalidA2ARegistry("Agent Card skill IDs must be unique")
            seen_skills.add(normalized_id)
            input_modes = item.get("inputModes", raw_card["defaultInputModes"])
            output_modes = item.get("outputModes", raw_card["defaultOutputModes"])
            if not isinstance(input_modes, list) or not isinstance(output_modes, list):
                raise InvalidA2ARegistry("Agent Card skill modes must be arrays")
            skills.append(
                A2ASkillCandidate(
                    skill_id=normalized_id,
                    name=skill_name.strip(),
                    description=skill_description.strip(),
                    tags=tuple(tag.strip() for tag in tags if tag.strip()),
                    input_modes=tuple(str(value) for value in input_modes),
                    output_modes=tuple(str(value) for value in output_modes),
                )
            )
        now = utc_now()
        signatures = raw_card.get("signatures", [])
        if not isinstance(signatures, list):
            raise InvalidA2ARegistry("Agent Card signatures must be an array")
        signature_status = (
            AgentCardSignatureStatus.PRESENT_UNVERIFIED
            if isinstance(signatures, list) and signatures
            else AgentCardSignatureStatus.UNSIGNED
        )
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            peer_id=peer.id,
            digest=f"sha256:{hashlib.sha256(encoded_card).hexdigest()}",
            raw_card=json.loads(encoded_card),
            agent_name=name.strip(),
            agent_description=description.strip(),
            agent_version=agent_version.strip(),
            endpoints=tuple(endpoints),
            skills=tuple(skills),
            capabilities=dict(capabilities),
            security_schemes=dict(security_schemes),
            signature_status=signature_status,
            fetched_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            source_etag=source_etag.strip() if source_etag and source_etag.strip() else None,
            source=source,
            source_url=source_url.strip() if source_url and source_url.strip() else None,
        )
