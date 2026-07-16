from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.artifacts import (
    Artifact,
    ArtifactAggregate,
    ArtifactClassification,
    ArtifactRef,
    ArtifactVersion,
)
from agentmesh.domain.errors import (
    ArtifactNotFound,
    ArtifactVersionNotFound,
    IdempotencyConflict,
    InvalidArtifact,
)
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope


class ArtifactService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        owner_id: str,
        max_inline_bytes: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id.strip()
        self._owner_id = owner_id.strip()
        self._max_inline_bytes = max_inline_bytes
        if not self._tenant_id or not self._owner_id:
            raise InvalidArtifact("Artifact service tenant and owner must not be empty")
        if self._max_inline_bytes < 1:
            raise InvalidArtifact("Artifact inline size limit must be positive")

    @property
    def max_inline_bytes(self) -> int:
        return self._max_inline_bytes

    def create_artifact(
        self,
        *,
        display_name: str,
        kind: str,
        classification: ArtifactClassification,
        media_type: str,
        content: bytes,
        expected_sha256: str | None = None,
        producer_run_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> ArtifactAggregate:
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        scope = f"create-artifact:{self._tenant_id}"
        request_hash = self._request_hash(
            {
                "display_name": display_name.strip(),
                "kind": kind.strip().lower(),
                "classification": classification.value,
                "media_type": media_type.strip().lower(),
                "content_sha256": sha256(content).hexdigest(),
                "expected_sha256": (expected_sha256.strip().lower() if expected_sha256 else None),
                "producer_run_id": str(producer_run_id) if producer_run_id else None,
            }
        )

        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return replay
            self._validate_producer_run(uow, producer_run_id)

            artifact = Artifact.create(
                tenant_id=self._tenant_id,
                owner_id=self._owner_id,
                display_name=display_name,
                kind=kind,
                classification=classification,
            )
            version = self._build_version(
                artifact,
                media_type=media_type,
                content=content,
                expected_sha256=expected_sha256,
                producer_run_id=producer_run_id,
            )
            uow.artifacts.add(artifact)
            uow.artifact_versions.add(version)
            uow.outbox.add(self._artifact_created_event(artifact))
            uow.outbox.add(self._version_available_event(artifact, version))
            self._save_idempotency_result(
                uow,
                scope,
                normalized_key,
                request_hash,
                artifact,
                version,
            )
            uow.commit()
            return ArtifactAggregate(artifact=artifact, versions=[version])

    def add_version(
        self,
        artifact_id: UUID,
        *,
        media_type: str,
        content: bytes,
        expected_sha256: str | None = None,
        producer_run_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> ArtifactAggregate:
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        scope = f"create-artifact-version:{self._tenant_id}:{artifact_id}"
        request_hash = self._request_hash(
            {
                "artifact_id": str(artifact_id),
                "media_type": media_type.strip().lower(),
                "content_sha256": sha256(content).hexdigest(),
                "expected_sha256": (expected_sha256.strip().lower() if expected_sha256 else None),
                "producer_run_id": str(producer_run_id) if producer_run_id else None,
            }
        )

        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return replay
            artifact = self._artifact_or_raise(uow, artifact_id, for_update=True)
            self._validate_producer_run(uow, producer_run_id)
            version = self._build_version(
                artifact,
                media_type=media_type,
                content=content,
                expected_sha256=expected_sha256,
                producer_run_id=producer_run_id,
            )
            uow.artifacts.save(artifact)
            uow.artifact_versions.add(version)
            uow.outbox.add(self._version_available_event(artifact, version))
            self._save_idempotency_result(
                uow,
                scope,
                normalized_key,
                request_hash,
                artifact,
                version,
            )
            uow.commit()
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: UUID) -> ArtifactAggregate:
        with self._uow_factory() as uow:
            artifact = self._artifact_or_raise(uow, artifact_id)
            return ArtifactAggregate(
                artifact=artifact,
                versions=uow.artifact_versions.list_for_artifact(artifact.id),
            )

    def list_artifacts(self, *, limit: int, offset: int) -> list[ArtifactAggregate]:
        with self._uow_factory() as uow:
            artifacts = uow.artifacts.list(
                tenant_id=self._tenant_id,
                limit=limit,
                offset=offset,
            )
            return [
                ArtifactAggregate(
                    artifact=artifact,
                    versions=uow.artifact_versions.list_for_artifact(artifact.id),
                )
                for artifact in artifacts
            ]

    def get_version_content(self, version_id: UUID) -> tuple[Artifact, ArtifactVersion]:
        with self._uow_factory() as uow:
            version = uow.artifact_versions.get(version_id)
            if version is None:
                raise ArtifactVersionNotFound(str(version_id))
            artifact = self._artifact_or_raise(uow, version.artifact_id)
            return artifact, version

    def _build_version(
        self,
        artifact: Artifact,
        *,
        media_type: str,
        content: bytes,
        expected_sha256: str | None,
        producer_run_id: UUID | None,
    ) -> ArtifactVersion:
        version_number = artifact.reserve_version()
        return ArtifactVersion.create_inline(
            artifact_id=artifact.id,
            version_number=version_number,
            media_type=media_type,
            content=content,
            max_inline_bytes=self._max_inline_bytes,
            expected_sha256=expected_sha256,
            producer_run_id=producer_run_id,
        )

    def _artifact_or_raise(
        self,
        uow: Any,
        artifact_id: UUID,
        *,
        for_update: bool = False,
    ) -> Artifact:
        artifact = uow.artifacts.get(artifact_id, for_update=for_update)
        if artifact is None or artifact.tenant_id != self._tenant_id:
            raise ArtifactNotFound(str(artifact_id))
        return artifact

    def _validate_producer_run(self, uow: Any, producer_run_id: UUID | None) -> None:
        if producer_run_id is None:
            return
        run = uow.runs.get(producer_run_id)
        task = uow.tasks.get(run.task_id) if run is not None else None
        if task is None or task.tenant_id != self._tenant_id:
            raise InvalidArtifact("producer_run_id does not reference a Run in this tenant")

    def _idempotent_replay(
        self,
        uow: Any,
        scope: str,
        key: str | None,
        request_hash: str,
    ) -> ArtifactAggregate | None:
        if key is None:
            return None
        uow.idempotency.lock(scope, key)
        existing = uow.idempotency.get(scope, key)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                "Idempotency-Key was already used for different Artifact content or metadata"
            )
        artifact_id = UUID(str(existing.result["artifact_id"]))
        return ArtifactAggregate(
            artifact=self._artifact_or_raise(uow, artifact_id),
            versions=uow.artifact_versions.list_for_artifact(artifact_id),
        )

    @staticmethod
    def _normalize_idempotency_key(value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        if value is not None and not normalized:
            raise InvalidArtifact("Idempotency-Key must not be blank")
        return normalized

    @staticmethod
    def _request_hash(value: dict[str, Any]) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _save_idempotency_result(
        uow: Any,
        scope: str,
        key: str | None,
        request_hash: str,
        artifact: Artifact,
        version: ArtifactVersion,
    ) -> None:
        if key is None:
            return
        uow.idempotency.add(
            IdempotencyRecord.create(
                scope=scope,
                key=key,
                request_hash=request_hash,
                result={"artifact_id": str(artifact.id), "version_id": str(version.id)},
            )
        )

    @staticmethod
    def _artifact_created_event(artifact: Artifact) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name="agentmesh.artifact.created",
            tenant_id=artifact.tenant_id,
            aggregate_id=artifact.id,
            payload={
                "artifact_id": str(artifact.id),
                "kind": artifact.kind,
                "classification": artifact.classification.value,
            },
        )

    @staticmethod
    def _version_available_event(
        artifact: Artifact,
        version: ArtifactVersion,
    ) -> MessageEnvelope:
        artifact_ref = ArtifactRef.from_entities(artifact, version)
        return MessageEnvelope.domain_event(
            schema_name="agentmesh.artifact-version.available",
            tenant_id=artifact.tenant_id,
            aggregate_id=artifact.id,
            payload={
                "artifact_id": str(artifact_ref.artifact_id),
                "version_id": str(artifact_ref.version_id),
                "version_number": artifact_ref.version_number,
                "media_type": artifact_ref.media_type,
                "kind": artifact_ref.kind,
                "size_bytes": artifact_ref.size_bytes,
                "sha256": artifact_ref.sha256,
                "storage_class": artifact_ref.storage_class.value,
                "classification": artifact_ref.classification.value,
                "scan_status": artifact_ref.scan_status.value,
                "producer_run_id": (
                    str(artifact_ref.producer_run_id) if artifact_ref.producer_run_id else None
                ),
            },
        )
