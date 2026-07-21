from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from agentmesh.domain.quotas import QuotaPolicy, QuotaReservation, QuotaScope
from agentmesh.infrastructure.postgres.models import QuotaPolicyRecord, QuotaReservationRecord


class SqlAlchemyQuotaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_policy(self, policy: QuotaPolicy) -> None:
        self._session.add(self._policy_record(policy))

    def replace_active(self, policy: QuotaPolicy) -> None:
        statement = update(QuotaPolicyRecord).where(
            QuotaPolicyRecord.tenant_id == policy.tenant_id,
            QuotaPolicyRecord.scope == policy.scope.value,
            QuotaPolicyRecord.active.is_(True),
        )
        if policy.scope is QuotaScope.PROJECT:
            statement = statement.where(QuotaPolicyRecord.project_id == policy.project_id)
        else:
            statement = statement.where(QuotaPolicyRecord.project_id.is_(None))
        self._session.execute(statement.values(active=False))
        self.add_policy(policy)

    def get_active(
        self,
        tenant_id: str,
        scope: QuotaScope,
        project_id: str | None,
        *,
        for_update: bool = False,
    ) -> QuotaPolicy | None:
        statement: Select[tuple[QuotaPolicyRecord]] = select(QuotaPolicyRecord).where(
            QuotaPolicyRecord.tenant_id == tenant_id,
            QuotaPolicyRecord.scope == scope.value,
            QuotaPolicyRecord.active.is_(True),
        )
        statement = statement.where(
            QuotaPolicyRecord.project_id == project_id
            if project_id is not None
            else QuotaPolicyRecord.project_id.is_(None)
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return self._policy(record) if record is not None else None

    def list_active_for_task(
        self, tenant_id: str, project_id: str, *, for_update: bool = False
    ) -> list[QuotaPolicy]:
        statement: Select[tuple[QuotaPolicyRecord]] = (
            select(QuotaPolicyRecord)
            .where(
                QuotaPolicyRecord.tenant_id == tenant_id,
                QuotaPolicyRecord.active.is_(True),
                (QuotaPolicyRecord.scope == QuotaScope.TENANT.value)
                | (
                    (QuotaPolicyRecord.scope == QuotaScope.PROJECT.value)
                    & (QuotaPolicyRecord.project_id == project_id)
                ),
            )
            .order_by(QuotaPolicyRecord.scope, QuotaPolicyRecord.id)
        )
        if for_update:
            statement = statement.with_for_update()
        return [self._policy(record) for record in self._session.scalars(statement)]

    def list_active(self, tenant_id: str) -> list[QuotaPolicy]:
        statement = (
            select(QuotaPolicyRecord)
            .where(QuotaPolicyRecord.tenant_id == tenant_id, QuotaPolicyRecord.active.is_(True))
            .order_by(QuotaPolicyRecord.scope, QuotaPolicyRecord.project_id)
        )
        return [self._policy(record) for record in self._session.scalars(statement)]

    def next_version(self, tenant_id: str, scope: QuotaScope, project_id: str | None) -> int:
        statement = select(func.coalesce(func.max(QuotaPolicyRecord.version), 0)).where(
            QuotaPolicyRecord.tenant_id == tenant_id,
            QuotaPolicyRecord.scope == scope.value,
        )
        statement = statement.where(
            QuotaPolicyRecord.project_id == project_id
            if project_id is not None
            else QuotaPolicyRecord.project_id.is_(None)
        )
        return int(self._session.scalar(statement) or 0) + 1

    def count_active(self, policy_id: UUID) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(QuotaReservationRecord)
                .where(
                    QuotaReservationRecord.policy_id == policy_id,
                    QuotaReservationRecord.released_at.is_(None),
                )
            )
            or 0
        )

    def count_active_for_scope(
        self, tenant_id: str, scope: QuotaScope, project_id: str | None
    ) -> int:
        statement = (
            select(func.count())
            .select_from(QuotaReservationRecord)
            .join(QuotaPolicyRecord, QuotaPolicyRecord.id == QuotaReservationRecord.policy_id)
            .where(
                QuotaReservationRecord.released_at.is_(None),
                QuotaPolicyRecord.tenant_id == tenant_id,
                QuotaPolicyRecord.scope == scope.value,
            )
        )
        statement = statement.where(
            QuotaPolicyRecord.project_id == project_id
            if project_id is not None
            else QuotaPolicyRecord.project_id.is_(None)
        )
        return int(self._session.scalar(statement) or 0)

    def add_reservation(self, reservation: QuotaReservation) -> None:
        self._session.add(QuotaReservationRecord(**reservation.__dict__))

    def list_reservations_for_attempt(
        self, attempt_id: UUID, *, for_update: bool = False
    ) -> list[QuotaReservation]:
        statement = (
            select(QuotaReservationRecord)
            .where(
                QuotaReservationRecord.attempt_id == attempt_id,
                QuotaReservationRecord.released_at.is_(None),
            )
            .order_by(QuotaReservationRecord.policy_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return [self._reservation(record) for record in self._session.scalars(statement)]

    def save_reservation(self, reservation: QuotaReservation) -> None:
        record = self._session.get(QuotaReservationRecord, reservation.id)
        if record is None:
            raise LookupError(f"Quota reservation {reservation.id} was not found")
        record.released_at = reservation.released_at

    @staticmethod
    def _policy_record(value: QuotaPolicy) -> QuotaPolicyRecord:
        return QuotaPolicyRecord(**value.__dict__)

    @staticmethod
    def _policy(value: QuotaPolicyRecord) -> QuotaPolicy:
        return QuotaPolicy(
            id=value.id,
            tenant_id=value.tenant_id,
            scope=QuotaScope(value.scope),
            project_id=value.project_id,
            max_concurrent_attempts=value.max_concurrent_attempts,
            weight=value.weight,
            version=value.version,
            active=value.active,
            created_by=value.created_by,
            created_at=value.created_at,
        )

    @staticmethod
    def _reservation(value: QuotaReservationRecord) -> QuotaReservation:
        return QuotaReservation(
            id=value.id,
            policy_id=value.policy_id,
            attempt_id=value.attempt_id,
            tenant_id=value.tenant_id,
            project_id=value.project_id,
            acquired_at=value.acquired_at,
            released_at=value.released_at,
        )
