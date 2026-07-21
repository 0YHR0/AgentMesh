from __future__ import annotations

from typing import Any

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.quotas import QuotaPolicy, QuotaPolicyStatus, QuotaReservation, QuotaScope
from agentmesh.domain.tasks import Task, TaskAttempt


class QuotaAdmissionRejected(Exception):
    def __init__(self, policy: QuotaPolicy) -> None:
        self.policy = policy
        super().__init__(
            f"{policy.scope.value.lower()} quota '{policy.scope_key}' has reached "
            f"{policy.max_concurrent_attempts} concurrent Attempts"
        )


class QuotaController:
    @staticmethod
    def reserve_attempt(uow: Any, task: Task, attempt: TaskAttempt) -> None:
        policies = uow.quotas.list_active_for_task(task.tenant_id, task.project_id, for_update=True)
        for policy in policies:
            used = uow.quotas.count_active_for_scope(
                policy.tenant_id, policy.scope, policy.project_id
            )
            if used >= policy.max_concurrent_attempts:
                raise QuotaAdmissionRejected(policy)
        for policy in policies:
            uow.quotas.add_reservation(
                QuotaReservation.acquire(
                    policy_id=policy.id,
                    attempt_id=attempt.id,
                    tenant_id=task.tenant_id,
                    project_id=task.project_id,
                )
            )

    @staticmethod
    def release_attempt(uow: Any, attempt: TaskAttempt) -> None:
        for reservation in uow.quotas.list_reservations_for_attempt(attempt.id, for_update=True):
            reservation.release()
            uow.quotas.save_reservation(reservation)


class QuotaPolicyService:
    def __init__(self, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def put_policy(
        self,
        *,
        scope: QuotaScope,
        project_id: str | None,
        max_concurrent_attempts: int,
        weight: int,
        created_by: str,
    ) -> QuotaPolicyStatus:
        with self._uow_factory() as uow:
            scope_key = f"{scope.value}:{project_id or self._tenant_id}"
            uow.idempotency.lock("quota-policy", f"{self._tenant_id}:{scope_key}")
            current = uow.quotas.get_active(self._tenant_id, scope, project_id, for_update=True)
            version = (
                current.version + 1
                if current is not None
                else uow.quotas.next_version(self._tenant_id, scope, project_id)
            )
            policy = QuotaPolicy.create(
                tenant_id=self._tenant_id,
                scope=scope,
                project_id=project_id,
                max_concurrent_attempts=max_concurrent_attempts,
                weight=weight,
                version=version,
                created_by=created_by,
            )
            uow.quotas.replace_active(policy)
            active_reservations = uow.quotas.count_active_for_scope(
                policy.tenant_id, policy.scope, policy.project_id
            )
            uow.commit()
            return QuotaPolicyStatus(
                policy=policy, active_reservations=active_reservations
            )

    def list_status(self) -> tuple[QuotaPolicyStatus, ...]:
        with self._uow_factory() as uow:
            return tuple(
                QuotaPolicyStatus(
                    policy=value,
                    active_reservations=uow.quotas.count_active_for_scope(
                        value.tenant_id, value.scope, value.project_id
                    ),
                )
                for value in uow.quotas.list_active(self._tenant_id)
            )
