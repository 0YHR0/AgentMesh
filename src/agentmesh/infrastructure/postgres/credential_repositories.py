from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.credentials import (
    CredentialBinding,
    CredentialBindingStatus,
    CredentialLease,
    CredentialLeaseStatus,
    SecretProvider,
    SecretPurpose,
    SecretReference,
    SecretReferenceStatus,
)
from agentmesh.infrastructure.postgres.models import (
    CredentialBindingRecord,
    CredentialLeaseRecord,
    SecretReferenceRecord,
)


class SqlAlchemyCredentialRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_secret_reference(self, reference: SecretReference) -> None:
        self._session.add(_secret_record(reference))

    def get_secret_reference(
        self, reference_id: UUID, *, for_update: bool = False
    ) -> SecretReference | None:
        value = self._session.get(SecretReferenceRecord, reference_id, with_for_update=for_update)
        return _secret_domain(value) if value is not None else None

    def save_secret_reference(self, reference: SecretReference) -> None:
        value = self._session.get(SecretReferenceRecord, reference.id)
        if value is None:
            raise LookupError(reference.id)
        value.status = reference.status.value
        value.updated_at = reference.updated_at
        value.revision = reference.revision

    def list_secret_references(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[SecretReference]:
        values = self._session.scalars(
            select(SecretReferenceRecord)
            .where(SecretReferenceRecord.tenant_id == tenant_id)
            .order_by(SecretReferenceRecord.created_at.desc(), SecretReferenceRecord.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [_secret_domain(value) for value in values]

    def add_binding(self, binding: CredentialBinding) -> None:
        self._session.add(_binding_record(binding))

    def get_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> CredentialBinding | None:
        value = self._session.get(CredentialBindingRecord, binding_id, with_for_update=for_update)
        return _binding_domain(value) if value is not None else None

    def save_binding(self, binding: CredentialBinding) -> None:
        value = self._session.get(CredentialBindingRecord, binding.id)
        if value is None:
            raise LookupError(binding.id)
        value.status = binding.status.value
        value.updated_at = binding.updated_at
        value.revision = binding.revision

    def list_bindings(self, *, tenant_id: str, limit: int, offset: int) -> list[CredentialBinding]:
        values = self._session.scalars(
            select(CredentialBindingRecord)
            .where(CredentialBindingRecord.tenant_id == tenant_id)
            .order_by(CredentialBindingRecord.created_at.desc(), CredentialBindingRecord.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [_binding_domain(value) for value in values]

    def add_lease(self, lease: CredentialLease) -> None:
        self._session.add(_lease_record(lease))

    def get_lease(self, lease_id: UUID, *, for_update: bool = False) -> CredentialLease | None:
        value = self._session.get(CredentialLeaseRecord, lease_id, with_for_update=for_update)
        return _lease_domain(value) if value is not None else None

    def save_lease(self, lease: CredentialLease) -> None:
        value = self._session.get(CredentialLeaseRecord, lease.id)
        if value is None:
            raise LookupError(lease.id)
        value.status = lease.status.value
        value.issued_at = lease.issued_at
        value.completed_at = lease.completed_at
        value.error = lease.error
        value.updated_at = lease.updated_at
        value.revision = lease.revision

    def list_leases(self, *, tenant_id: str, limit: int, offset: int) -> list[CredentialLease]:
        values = self._session.scalars(
            select(CredentialLeaseRecord)
            .where(CredentialLeaseRecord.tenant_id == tenant_id)
            .order_by(CredentialLeaseRecord.created_at.desc(), CredentialLeaseRecord.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [_lease_domain(value) for value in values]


def _secret_record(value: SecretReference) -> SecretReferenceRecord:
    return SecretReferenceRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        provider=value.provider.value,
        external_key=value.external_key,
        version_selector=value.version_selector,
        purpose=value.purpose.value,
        allowed_audiences=list(value.allowed_audiences),
        status=value.status.value,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _secret_domain(value: SecretReferenceRecord) -> SecretReference:
    return SecretReference(
        id=value.id,
        tenant_id=value.tenant_id,
        provider=SecretProvider(value.provider),
        external_key=value.external_key,
        version_selector=value.version_selector,
        purpose=SecretPurpose(value.purpose),
        allowed_audiences=tuple(value.allowed_audiences),
        status=SecretReferenceStatus(value.status),
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _binding_record(value: CredentialBinding) -> CredentialBindingRecord:
    return CredentialBindingRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        workload_principal_id=value.workload_principal_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        card_digest=value.card_digest,
        secret_reference_id=value.secret_reference_id,
        scheme_name=value.scheme_name,
        auth_scheme=value.auth_scheme,
        audience=value.audience,
        scopes=list(value.scopes),
        environment=value.environment,
        expires_at=value.expires_at,
        status=value.status.value,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _binding_domain(value: CredentialBindingRecord) -> CredentialBinding:
    return CredentialBinding(
        id=value.id,
        tenant_id=value.tenant_id,
        workload_principal_id=value.workload_principal_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        card_digest=value.card_digest,
        secret_reference_id=value.secret_reference_id,
        scheme_name=value.scheme_name,
        auth_scheme=value.auth_scheme,
        audience=value.audience,
        scopes=tuple(value.scopes),
        environment=value.environment,
        expires_at=value.expires_at,
        status=CredentialBindingStatus(value.status),
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _lease_record(value: CredentialLease) -> CredentialLeaseRecord:
    return CredentialLeaseRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        binding_id=value.binding_id,
        secret_reference_id=value.secret_reference_id,
        workload_principal_id=value.workload_principal_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        task_id=value.task_id,
        run_id=value.run_id,
        audience=value.audience,
        scopes=list(value.scopes),
        status=value.status.value,
        issued_at=value.issued_at,
        expires_at=value.expires_at,
        completed_at=value.completed_at,
        error=value.error,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _lease_domain(value: CredentialLeaseRecord) -> CredentialLease:
    return CredentialLease(
        id=value.id,
        tenant_id=value.tenant_id,
        binding_id=value.binding_id,
        secret_reference_id=value.secret_reference_id,
        workload_principal_id=value.workload_principal_id,
        peer_id=value.peer_id,
        card_snapshot_id=value.card_snapshot_id,
        task_id=value.task_id,
        run_id=value.run_id,
        audience=value.audience,
        scopes=tuple(value.scopes),
        status=CredentialLeaseStatus(value.status),
        issued_at=value.issued_at,
        expires_at=value.expires_at,
        completed_at=value.completed_at,
        error=value.error,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )
