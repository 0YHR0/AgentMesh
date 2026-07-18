from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from agentmesh.domain.identity import (
    ExternalIdentity,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    RoleBindingStatus,
)
from agentmesh.infrastructure.postgres.models import (
    ExternalIdentityRecord,
    PrincipalRecord,
    RoleBindingRecord,
)


class SqlAlchemyIdentityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_principal(self, principal: Principal) -> None:
        self._session.add(_principal_record(principal))

    def get_principal(self, principal_id: UUID, *, for_update: bool = False) -> Principal | None:
        statement: Select[tuple[PrincipalRecord]] = select(PrincipalRecord).where(
            PrincipalRecord.id == principal_id
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return _principal(record) if record is not None else None

    def save_principal(self, principal: Principal) -> None:
        record = self._session.get(PrincipalRecord, principal.id)
        if record is None:
            raise LookupError(principal.id)
        record.status = principal.status.value
        record.display_name = principal.display_name
        record.updated_at = principal.updated_at
        record.revision = principal.revision

    def list_principals(self, *, tenant_id: str, limit: int, offset: int) -> list[Principal]:
        records = self._session.scalars(
            select(PrincipalRecord)
            .where(PrincipalRecord.tenant_id == tenant_id)
            .order_by(PrincipalRecord.created_at, PrincipalRecord.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [_principal(record) for record in records]

    def add_external_identity(self, identity: ExternalIdentity) -> None:
        self._session.add(
            ExternalIdentityRecord(
                id=identity.id,
                tenant_id=identity.tenant_id,
                principal_id=identity.principal_id,
                issuer=identity.issuer,
                subject=identity.subject,
                created_at=identity.created_at,
                created_by=identity.created_by,
            )
        )

    def get_external_identity(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> ExternalIdentity | None:
        record = self._session.scalar(
            select(ExternalIdentityRecord).where(
                ExternalIdentityRecord.tenant_id == tenant_id,
                ExternalIdentityRecord.issuer == issuer,
                ExternalIdentityRecord.subject == subject,
            )
        )
        return _external_identity(record) if record is not None else None

    def list_external_identities(self, principal_id: UUID) -> list[ExternalIdentity]:
        records = self._session.scalars(
            select(ExternalIdentityRecord)
            .where(ExternalIdentityRecord.principal_id == principal_id)
            .order_by(ExternalIdentityRecord.created_at, ExternalIdentityRecord.id)
        ).all()
        return [_external_identity(record) for record in records]

    def add_role_binding(self, binding: RoleBinding) -> None:
        self._session.add(_role_binding_record(binding))

    def get_role_binding(self, binding_id: UUID, *, for_update: bool = False) -> RoleBinding | None:
        statement: Select[tuple[RoleBindingRecord]] = select(RoleBindingRecord).where(
            RoleBindingRecord.id == binding_id
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return _role_binding(record) if record is not None else None

    def save_role_binding(self, binding: RoleBinding) -> None:
        record = self._session.get(RoleBindingRecord, binding.id)
        if record is None:
            raise LookupError(binding.id)
        record.status = binding.status.value
        record.revoked_at = binding.revoked_at
        record.revoked_by = binding.revoked_by
        record.revoke_reason = binding.revoke_reason
        record.revision = binding.revision

    def list_role_bindings(self, principal_id: UUID) -> list[RoleBinding]:
        records = self._session.scalars(
            select(RoleBindingRecord)
            .where(RoleBindingRecord.principal_id == principal_id)
            .order_by(RoleBindingRecord.created_at, RoleBindingRecord.id)
        ).all()
        return [_role_binding(record) for record in records]


def _principal_record(value: Principal) -> PrincipalRecord:
    return PrincipalRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        principal_type=value.principal_type.value,
        status=value.status.value,
        display_name=value.display_name,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _principal(value: PrincipalRecord) -> Principal:
    return Principal(
        id=value.id,
        tenant_id=value.tenant_id,
        principal_type=PrincipalType(value.principal_type),
        status=PrincipalStatus(value.status),
        display_name=value.display_name,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _external_identity(value: ExternalIdentityRecord) -> ExternalIdentity:
    return ExternalIdentity(
        id=value.id,
        tenant_id=value.tenant_id,
        principal_id=value.principal_id,
        issuer=value.issuer,
        subject=value.subject,
        created_at=value.created_at,
        created_by=value.created_by,
    )


def _role_binding_record(value: RoleBinding) -> RoleBindingRecord:
    return RoleBindingRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        principal_id=value.principal_id,
        role=value.role.value,
        status=value.status.value,
        effective_at=value.effective_at,
        expires_at=value.expires_at,
        created_at=value.created_at,
        created_by=value.created_by,
        revoked_at=value.revoked_at,
        revoked_by=value.revoked_by,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
    )


def _role_binding(value: RoleBindingRecord) -> RoleBinding:
    return RoleBinding(
        id=value.id,
        tenant_id=value.tenant_id,
        principal_id=value.principal_id,
        role=Role(value.role),
        status=RoleBindingStatus(value.status),
        effective_at=value.effective_at,
        expires_at=value.expires_at,
        created_at=value.created_at,
        created_by=value.created_by,
        revoked_at=value.revoked_at,
        revoked_by=value.revoked_by,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
    )
