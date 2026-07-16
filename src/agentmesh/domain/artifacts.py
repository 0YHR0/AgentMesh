from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import (
    ArtifactIntegrityMismatch,
    ArtifactTooLarge,
    InvalidArtifact,
)
from agentmesh.domain.tasks import utc_now

ARTIFACT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_INLINE_MEDIA_TYPES = frozenset({"application/json", "text/plain"})


class ArtifactClassification(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class ArtifactStorageClass(str, Enum):
    INLINE_SMALL = "INLINE_SMALL"


class ArtifactVersionStatus(str, Enum):
    AVAILABLE = "AVAILABLE"


class ArtifactScanStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class Artifact:
    id: UUID
    tenant_id: str
    owner_id: str
    display_name: str
    kind: str
    classification: ArtifactClassification
    version_count: int
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        owner_id: str,
        display_name: str,
        kind: str,
        classification: ArtifactClassification,
    ) -> Artifact:
        normalized_tenant = tenant_id.strip()
        normalized_owner = owner_id.strip()
        normalized_name = display_name.strip()
        normalized_kind = kind.strip().lower()
        if not normalized_tenant or not normalized_owner:
            raise InvalidArtifact("Artifact tenant and owner must not be empty")
        if not normalized_name or len(normalized_name) > 255:
            raise InvalidArtifact("Artifact display name must be 1-255 characters")
        if not ARTIFACT_KIND_PATTERN.fullmatch(normalized_kind):
            raise InvalidArtifact(
                "Artifact kind must be 2-64 lowercase letters, numbers, dots, "
                "dashes, or underscores"
            )
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=normalized_tenant,
            owner_id=normalized_owner,
            display_name=normalized_name,
            kind=normalized_kind,
            classification=classification,
            version_count=0,
            revision=1,
            created_at=now,
            updated_at=now,
        )

    def reserve_version(self) -> int:
        self.version_count += 1
        self.revision += 1
        self.updated_at = utc_now()
        return self.version_count


@dataclass(frozen=True)
class ArtifactVersion:
    id: UUID
    artifact_id: UUID
    version_number: int
    media_type: str
    size_bytes: int
    sha256: str
    storage_class: ArtifactStorageClass
    status: ArtifactVersionStatus
    scan_status: ArtifactScanStatus
    producer_run_id: UUID | None
    content: bytes
    created_at: datetime

    @classmethod
    def create_inline(
        cls,
        *,
        artifact_id: UUID,
        version_number: int,
        media_type: str,
        content: bytes,
        max_inline_bytes: int,
        expected_sha256: str | None = None,
        producer_run_id: UUID | None = None,
    ) -> ArtifactVersion:
        normalized_media_type = media_type.strip().lower()
        if normalized_media_type not in SUPPORTED_INLINE_MEDIA_TYPES:
            supported = ", ".join(sorted(SUPPORTED_INLINE_MEDIA_TYPES))
            raise InvalidArtifact(f"Inline Artifact media type must be one of: {supported}")
        if version_number < 1:
            raise InvalidArtifact("Artifact version number must be positive")
        if not content:
            raise InvalidArtifact("Artifact content must not be empty")
        if max_inline_bytes < 1:
            raise InvalidArtifact("Artifact inline size limit must be positive")
        if len(content) > max_inline_bytes:
            raise ArtifactTooLarge(len(content), max_inline_bytes)

        cls._validate_text_content(normalized_media_type, content)
        content_sha256 = sha256(content).hexdigest()
        if expected_sha256 is not None:
            normalized_expected = expected_sha256.strip().lower()
            if not SHA256_PATTERN.fullmatch(normalized_expected):
                raise InvalidArtifact("expected_sha256 must be 64 lowercase hexadecimal characters")
            if normalized_expected != content_sha256:
                raise ArtifactIntegrityMismatch(normalized_expected, content_sha256)

        return cls(
            id=uuid4(),
            artifact_id=artifact_id,
            version_number=version_number,
            media_type=normalized_media_type,
            size_bytes=len(content),
            sha256=content_sha256,
            storage_class=ArtifactStorageClass.INLINE_SMALL,
            status=ArtifactVersionStatus.AVAILABLE,
            scan_status=ArtifactScanStatus.NOT_CONFIGURED,
            producer_run_id=producer_run_id,
            content=bytes(content),
            created_at=utc_now(),
        )

    @staticmethod
    def _validate_text_content(media_type: str, content: bytes) -> None:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidArtifact("Inline Artifact content must be valid UTF-8") from exc
        if "\x00" in decoded:
            raise InvalidArtifact("Inline Artifact content must not contain NUL characters")
        if media_type == "application/json":
            try:
                value: Any = json.loads(decoded)
            except json.JSONDecodeError as exc:
                raise InvalidArtifact(
                    "application/json Artifact content must be valid JSON"
                ) from exc
            if value is None:
                raise InvalidArtifact("application/json Artifact content must not be null")


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: UUID
    version_id: UUID
    version_number: int
    media_type: str
    kind: str
    size_bytes: int
    sha256: str
    storage_class: ArtifactStorageClass
    classification: ArtifactClassification
    scan_status: ArtifactScanStatus
    producer_run_id: UUID | None

    @classmethod
    def from_entities(cls, artifact: Artifact, version: ArtifactVersion) -> ArtifactRef:
        if version.artifact_id != artifact.id:
            raise InvalidArtifact("Artifact Version belongs to another Artifact")
        return cls(
            artifact_id=artifact.id,
            version_id=version.id,
            version_number=version.version_number,
            media_type=version.media_type,
            kind=artifact.kind,
            size_bytes=version.size_bytes,
            sha256=version.sha256,
            storage_class=version.storage_class,
            classification=artifact.classification,
            scan_status=version.scan_status,
            producer_run_id=version.producer_run_id,
        )


@dataclass(frozen=True)
class ArtifactAggregate:
    artifact: Artifact
    versions: list[ArtifactVersion]
