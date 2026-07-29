from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from agentmesh.domain.organizational_memory import (
    MemoryEvidence,
    MemoryNamespaceType,
    MemoryPolicy,
    MemoryProvenanceType,
    MemoryRecord,
    MemoryRetrieval,
    MemoryReview,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from agentmesh.infrastructure.postgres.models import (
    MemoryEvidenceRecord,
    MemoryPolicyRecord,
    MemoryRecordModel,
    MemoryRetrievalRecord,
    MemoryReviewRecord,
)


class SqlAlchemyOrganizationalMemoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_policy(self, value: MemoryPolicy) -> None:
        self._session.add(MemoryPolicyRecord(**self._policy_data(value)))

    def get_policy(self, policy_id: UUID) -> MemoryPolicy | None:
        record = self._session.get(MemoryPolicyRecord, policy_id)
        return self._policy(record) if record else None

    def get_policy_by_key(
        self, company_id: UUID, key: str, *, active_only: bool = False
    ) -> MemoryPolicy | None:
        query = select(MemoryPolicyRecord).where(
            MemoryPolicyRecord.company_id == company_id,
            MemoryPolicyRecord.key == key,
        )
        if active_only:
            query = query.where(MemoryPolicyRecord.active.is_(True))
        record = self._session.scalar(
            query.order_by(MemoryPolicyRecord.version.desc())
        )
        return self._policy(record) if record else None

    def list_policies(self, company_id: UUID) -> list[MemoryPolicy]:
        records = self._session.scalars(
            select(MemoryPolicyRecord)
            .where(MemoryPolicyRecord.company_id == company_id)
            .order_by(MemoryPolicyRecord.key, MemoryPolicyRecord.version.desc())
        )
        return [self._policy(record) for record in records]

    def save_policy(self, value: MemoryPolicy) -> None:
        record = self._required(MemoryPolicyRecord, value.id)
        for key, item in self._policy_data(value).items():
            if key != "id":
                setattr(record, key, item)

    def add_record(self, value: MemoryRecord) -> None:
        self._session.add(MemoryRecordModel(**self._memory_data(value)))

    def get_record(
        self, memory_id: UUID, *, for_update: bool = False
    ) -> MemoryRecord | None:
        record = self._session.get(
            MemoryRecordModel, memory_id, with_for_update=for_update
        )
        return self._memory(record) if record else None

    def find_by_digest(
        self,
        *,
        company_id: UUID,
        namespace_type: str,
        namespace_id: str,
        memory_type: MemoryType,
        content_digest: str,
        statuses: set[MemoryStatus],
    ) -> MemoryRecord | None:
        record = self._session.scalar(
            select(MemoryRecordModel).where(
                MemoryRecordModel.company_id == company_id,
                MemoryRecordModel.namespace_type == namespace_type,
                MemoryRecordModel.namespace_id == namespace_id,
                MemoryRecordModel.memory_type == memory_type.value,
                MemoryRecordModel.content_digest == content_digest,
                MemoryRecordModel.status.in_(
                    [status.value for status in statuses]
                ),
            )
        )
        return self._memory(record) if record else None

    def list_candidates(self, company_id: UUID) -> list[MemoryRecord]:
        records = self._session.scalars(
            select(MemoryRecordModel)
            .where(
                MemoryRecordModel.company_id == company_id,
                MemoryRecordModel.status == MemoryStatus.CANDIDATE.value,
            )
            .order_by(MemoryRecordModel.created_at)
        )
        return [self._memory(record) for record in records]

    def search_records(
        self,
        *,
        company_id: UUID,
        namespace_keys: list[str],
        memory_types: list[MemoryType],
    ) -> list[MemoryRecord]:
        namespaces = [
            (key.split("/", 1)[0].upper(), key.split("/", 1)[1])
            for key in namespace_keys
        ]
        records = self._session.scalars(
            select(MemoryRecordModel).where(
                MemoryRecordModel.company_id == company_id,
                tuple_(
                    MemoryRecordModel.namespace_type,
                    MemoryRecordModel.namespace_id,
                ).in_(namespaces),
                MemoryRecordModel.memory_type.in_(
                    [memory_type.value for memory_type in memory_types]
                ),
                MemoryRecordModel.status.in_(
                    [
                        MemoryStatus.ACCEPTED.value,
                        MemoryStatus.REVOKED.value,
                        MemoryStatus.SUPERSEDED.value,
                        MemoryStatus.EXPIRED.value,
                    ]
                ),
            )
        )
        return [self._memory(record) for record in records]

    def save_record(self, value: MemoryRecord) -> None:
        record = self._required(MemoryRecordModel, value.id)
        for key, item in self._memory_data(value).items():
            if key not in {"id", "company_id", "created_at"}:
                setattr(record, key, item)

    def add_evidence(self, value: MemoryEvidence) -> None:
        self._session.add(MemoryEvidenceRecord(**value.__dict__))

    def list_evidence(self, memory_id: UUID) -> list[MemoryEvidence]:
        records = self._session.scalars(
            select(MemoryEvidenceRecord)
            .where(MemoryEvidenceRecord.memory_id == memory_id)
            .order_by(MemoryEvidenceRecord.created_at)
        )
        return [
            MemoryEvidence(**self._record_data(record)) for record in records
        ]

    def add_review(self, value: MemoryReview) -> None:
        self._session.add(MemoryReviewRecord(**value.__dict__))

    def list_reviews(self, memory_id: UUID) -> list[MemoryReview]:
        records = self._session.scalars(
            select(MemoryReviewRecord)
            .where(MemoryReviewRecord.memory_id == memory_id)
            .order_by(MemoryReviewRecord.created_at)
        )
        return [MemoryReview(**self._record_data(record)) for record in records]

    def add_retrieval(self, value: MemoryRetrieval) -> None:
        data = dict(value.__dict__)
        data["memory_types"] = [item.value for item in value.memory_types]
        data["result_memory_ids"] = [
            str(item) for item in value.result_memory_ids
        ]
        self._session.add(MemoryRetrievalRecord(**data))

    def list_retrievals(
        self, *, task_id: UUID | None = None, run_id: UUID | None = None
    ) -> list[MemoryRetrieval]:
        query = select(MemoryRetrievalRecord)
        if task_id is not None:
            query = query.where(MemoryRetrievalRecord.task_id == task_id)
        if run_id is not None:
            query = query.where(MemoryRetrievalRecord.run_id == run_id)
        records = self._session.scalars(
            query.order_by(MemoryRetrievalRecord.created_at)
        )
        return [self._retrieval(record) for record in records]

    @staticmethod
    def _policy_data(value: MemoryPolicy) -> dict:
        data = dict(value.__dict__)
        data["allowed_memory_types"] = [
            item.value for item in value.allowed_memory_types
        ]
        data["auto_accept_memory_types"] = [
            item.value for item in value.auto_accept_memory_types
        ]
        data["forbidden_sensitivity_levels"] = [
            item.value for item in value.forbidden_sensitivity_levels
        ]
        return data

    @staticmethod
    def _memory_data(value: MemoryRecord) -> dict:
        data = dict(value.__dict__)
        for key in (
            "namespace_type",
            "memory_type",
            "provenance_type",
            "sensitivity",
            "status",
        ):
            data[key] = data[key].value
        return data

    @staticmethod
    def _record_data(record) -> dict:
        data = dict(record.__dict__)
        data.pop("_sa_instance_state", None)
        return data

    @classmethod
    def _policy(cls, record: MemoryPolicyRecord) -> MemoryPolicy:
        data = cls._record_data(record)
        data["allowed_memory_types"] = [
            MemoryType(item) for item in record.allowed_memory_types
        ]
        data["auto_accept_memory_types"] = [
            MemoryType(item) for item in record.auto_accept_memory_types
        ]
        data["forbidden_sensitivity_levels"] = [
            MemorySensitivity(item)
            for item in record.forbidden_sensitivity_levels
        ]
        return MemoryPolicy(**data)

    @classmethod
    def _memory(cls, record: MemoryRecordModel) -> MemoryRecord:
        data = cls._record_data(record)
        data["namespace_type"] = MemoryNamespaceType(record.namespace_type)
        data["memory_type"] = MemoryType(record.memory_type)
        data["provenance_type"] = MemoryProvenanceType(record.provenance_type)
        data["sensitivity"] = MemorySensitivity(record.sensitivity)
        data["status"] = MemoryStatus(record.status)
        return MemoryRecord(**data)

    @classmethod
    def _retrieval(cls, record: MemoryRetrievalRecord) -> MemoryRetrieval:
        data = cls._record_data(record)
        data["memory_types"] = [
            MemoryType(item) for item in record.memory_types
        ]
        data["result_memory_ids"] = [
            UUID(item) for item in record.result_memory_ids
        ]
        return MemoryRetrieval(**data)

    def _required(self, model, record_id):
        record = self._session.get(model, record_id)
        if record is None:
            raise LookupError(record_id)
        return record
