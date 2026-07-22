from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.artifacts import (
    Artifact,
    ArtifactClassification,
    ArtifactScanStatus,
    ArtifactStorageClass,
    ArtifactVersion,
    ArtifactVersionStatus,
)
from agentmesh.infrastructure.postgres.models import ArtifactRecord, ArtifactVersionRecord


class SqlAlchemyArtifactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, artifact: Artifact) -> None:
        self._session.add(
            ArtifactRecord(
                id=artifact.id,
                tenant_id=artifact.tenant_id,
                owner_id=artifact.owner_id,
                display_name=artifact.display_name,
                kind=artifact.kind,
                classification=artifact.classification.value,
                version_count=artifact.version_count,
                revision=artifact.revision,
                created_at=artifact.created_at,
                updated_at=artifact.updated_at,
            )
        )

    def get(self, artifact_id: UUID, *, for_update: bool = False) -> Artifact | None:
        record = self._session.get(ArtifactRecord, artifact_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Artifact]:
        statement = (
            select(ArtifactRecord)
            .where(ArtifactRecord.tenant_id == tenant_id)
            .order_by(ArtifactRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def save(self, artifact: Artifact) -> None:
        record = self._session.get(ArtifactRecord, artifact.id)
        if record is None:
            raise LookupError(artifact.id)
        record.display_name = artifact.display_name
        record.classification = artifact.classification.value
        record.version_count = artifact.version_count
        record.revision = artifact.revision
        record.updated_at = artifact.updated_at

    @staticmethod
    def _to_domain(record: ArtifactRecord) -> Artifact:
        return Artifact(
            id=record.id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            display_name=record.display_name,
            kind=record.kind,
            classification=ArtifactClassification(record.classification),
            version_count=record.version_count,
            revision=record.revision,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemyArtifactVersionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, version: ArtifactVersion) -> None:
        self._session.add(
            ArtifactVersionRecord(
                id=version.id,
                artifact_id=version.artifact_id,
                version_number=version.version_number,
                media_type=version.media_type,
                size_bytes=version.size_bytes,
                sha256=version.sha256,
                storage_class=version.storage_class.value,
                status=version.status.value,
                scan_status=version.scan_status.value,
                producer_run_id=version.producer_run_id,
                content=version.content,
                created_at=version.created_at,
            )
        )

    def get(self, version_id: UUID) -> ArtifactVersion | None:
        record = self._session.get(ArtifactVersionRecord, version_id)
        return self._to_domain(record) if record is not None else None

    def list_for_artifact(self, artifact_id: UUID) -> list[ArtifactVersion]:
        statement = (
            select(ArtifactVersionRecord)
            .where(ArtifactVersionRecord.artifact_id == artifact_id)
            .order_by(ArtifactVersionRecord.version_number.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_artifacts(self, artifact_ids: list[UUID]) -> list[ArtifactVersion]:
        if not artifact_ids:
            return []
        statement = (
            select(ArtifactVersionRecord)
            .where(ArtifactVersionRecord.artifact_id.in_(artifact_ids))
            .order_by(
                ArtifactVersionRecord.artifact_id.asc(),
                ArtifactVersionRecord.version_number.asc(),
            )
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_producer_runs(self, run_ids: list[UUID]) -> list[ArtifactVersion]:
        if not run_ids:
            return []
        statement = (
            select(ArtifactVersionRecord)
            .where(ArtifactVersionRecord.producer_run_id.in_(run_ids))
            .order_by(ArtifactVersionRecord.created_at.desc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_domain(record: ArtifactVersionRecord) -> ArtifactVersion:
        return ArtifactVersion(
            id=record.id,
            artifact_id=record.artifact_id,
            version_number=record.version_number,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            storage_class=ArtifactStorageClass(record.storage_class),
            status=ArtifactVersionStatus(record.status),
            scan_status=ArtifactScanStatus(record.scan_status),
            producer_run_id=record.producer_run_id,
            content=bytes(record.content),
            created_at=record.created_at,
        )
