"""PostgreSQL persistence for the framework-neutral runtime control plane."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeRegistration,
    RuntimeRegistrationStatus,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
    RuntimeVisibility,
)
from agentmesh.infrastructure.postgres.models import (
    RuntimeExecutionRecord,
    RuntimeLifecycleOperationRecord,
    RuntimeObservationRecord,
    RuntimeOwnershipHistoryRecord,
    RuntimeRegistrationRecord,
    RuntimeVersionRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


def _unfreeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _unfreeze(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_unfreeze(item) for item in value]
    return value


class SqlAlchemyRuntimeRepository:
    """Single repository; callers keep all mutations in one UoW transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_registration(self, value: RuntimeRegistration) -> None:
        self._session.add(RuntimeRegistrationRecord(**_registration_values(value)))

    def get_registration(
        self,
        registration_id: UUID,
        *,
        tenant_id: str,
        principal_id: UUID | None = None,
        for_update: bool = False,
    ) -> RuntimeRegistration | None:
        statement = select(RuntimeRegistrationRecord).where(
            RuntimeRegistrationRecord.id == registration_id,
            self._scope(RuntimeRegistrationRecord, tenant_id=tenant_id, principal_id=principal_id),
        )
        if for_update:
            statement = statement.with_for_update()
        return _registration_domain(self._session.scalar(statement))

    def get_registration_by_name(
        self,
        name: str,
        *,
        tenant_id: str,
        principal_id: UUID | None = None,
        for_update: bool = False,
    ) -> RuntimeRegistration | None:
        statement = select(RuntimeRegistrationRecord).where(
            RuntimeRegistrationRecord.name == name,
            self._scope(RuntimeRegistrationRecord, tenant_id=tenant_id, principal_id=principal_id),
        )
        statement = statement.order_by(
            (RuntimeRegistrationRecord.visibility == RuntimeVisibility.PLATFORM.value).asc(),
            RuntimeRegistrationRecord.created_at.desc(),
        )
        if for_update:
            statement = statement.with_for_update()
        return _registration_domain(self._session.scalar(statement))

    def list_registrations(
        self, *, tenant_id: str, principal_id: UUID | None = None, limit: int, offset: int
    ) -> list[RuntimeRegistration]:
        statement = (
            select(RuntimeRegistrationRecord)
            .where(
                self._scope(
                    RuntimeRegistrationRecord, tenant_id=tenant_id, principal_id=principal_id
                )
            )
            .order_by(RuntimeRegistrationRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_registration_domain(record) for record in self._session.scalars(statement)]

    def save_registration(
        self, value: RuntimeRegistration, *, tenant_id: str, principal_id: UUID | None = None
    ) -> None:
        self.get_registration(
            value.id, tenant_id=tenant_id, principal_id=principal_id, for_update=True
        )
        record = self._session.get(RuntimeRegistrationRecord, value.id)
        if record is None:
            raise LookupError(value.id)
        record.status = value.status.value
        record.default_version_id = value.default_version_id
        record.version = value.version
        record.updated_at = value.updated_at

    def add_version(self, value: RuntimeVersion) -> None:
        self._session.add(
            RuntimeVersionRecord(
                id=value.id,
                runtime_id=value.runtime_id,
                api_version=value.api_version,
                adapter_kind=value.adapter_kind,
                artifact_digest=value.artifact_digest,
                configuration_digest=value.configuration_digest,
                descriptor=_unfreeze(value.descriptor),
                trust_profile=value.trust_profile.value,
                compatibility=_unfreeze(value.compatibility),
                status=value.status.value,
                created_at=value.created_at,
                published_at=value.published_at,
                revoked_at=value.revoked_at,
            )
        )

    def get_version(
        self,
        version_id: UUID,
        *,
        tenant_id: str,
        principal_id: UUID | None = None,
        for_update: bool = False,
    ) -> RuntimeVersion | None:
        statement = (
            select(RuntimeVersionRecord)
            .join(
                RuntimeRegistrationRecord,
                RuntimeRegistrationRecord.id == RuntimeVersionRecord.runtime_id,
            )
            .where(
                RuntimeVersionRecord.id == version_id,
                self._scope(
                    RuntimeRegistrationRecord, tenant_id=tenant_id, principal_id=principal_id
                ),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        return _version_domain(self._session.scalar(statement))

    def list_versions(
        self, runtime_id: UUID, *, tenant_id: str, principal_id: UUID | None = None
    ) -> list[RuntimeVersion]:
        statement = (
            select(RuntimeVersionRecord)
            .join(
                RuntimeRegistrationRecord,
                RuntimeRegistrationRecord.id == RuntimeVersionRecord.runtime_id,
            )
            .where(
                RuntimeVersionRecord.runtime_id == runtime_id,
                self._scope(
                    RuntimeRegistrationRecord, tenant_id=tenant_id, principal_id=principal_id
                ),
            )
            .order_by(RuntimeVersionRecord.created_at.desc())
        )
        return [_version_domain(record) for record in self._session.scalars(statement)]

    def save_version(
        self, value: RuntimeVersion, *, tenant_id: str, principal_id: UUID | None = None
    ) -> None:
        self.get_version(value.id, tenant_id=tenant_id, principal_id=principal_id, for_update=True)
        record = self._session.get(RuntimeVersionRecord, value.id)
        if record is None:
            raise LookupError(value.id)
        record.status = value.status.value
        record.published_at = value.published_at
        record.revoked_at = value.revoked_at

    def add_execution(self, value: RuntimeExecution) -> None:
        self._session.add(RuntimeExecutionRecord(**_execution_values(value)))

    def get_execution(
        self, execution_id: UUID, *, tenant_id: str, for_update: bool = False
    ) -> RuntimeExecution | None:
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.id == execution_id,
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        return _execution_domain(self._session.scalar(statement))

    def get_execution_by_dispatch(
        self, dispatch_key: str, *, tenant_id: str, for_update: bool = False
    ) -> RuntimeExecution | None:
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.dispatch_key == dispatch_key,
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        return _execution_domain(self._session.scalar(statement))

    def get_active_or_unresolved_for_run(
        self, run_id: UUID, *, tenant_id: str, for_update: bool = False
    ) -> RuntimeExecution | None:
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.run_id == run_id,
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
                RuntimeExecutionRecord.phase.not_in(
                    ["SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT", "LOST"]
                ),
            )
            .order_by(RuntimeExecutionRecord.updated_at.desc())
        )
        if for_update:
            statement = statement.with_for_update()
        return _execution_domain(self._session.scalar(statement))

    def list_executions_for_run(self, run_id: UUID, *, tenant_id: str) -> list[RuntimeExecution]:
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.run_id == run_id,
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
            .order_by(RuntimeExecutionRecord.updated_at.desc())
        )
        return [_execution_domain(record) for record in self._session.scalars(statement)]

    def list_executions_for_tenant(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[RuntimeExecution]:
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
            .order_by(RuntimeExecutionRecord.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_execution_domain(record) for record in self._session.scalars(statement)]

    def save_execution(self, value: RuntimeExecution, *, tenant_id: str) -> None:
        self.get_execution(value.id, tenant_id=tenant_id, for_update=True)
        record = self._session.get(RuntimeExecutionRecord, value.id)
        if record is None:
            raise LookupError(value.id)
        for key, field_value in _execution_values(value).items():
            if key != "id":
                setattr(record, key, field_value)

    def claim_execution_owner(
        self,
        *,
        execution_id: UUID,
        tenant_id: str,
        attempt_id: UUID,
        fencing_token: int,
        expected_owner_attempt_id: UUID | None,
        expected_fencing_token: int | None,
        expected_version: int,
        now: Any,
        claim_reason: str,
        reattach_evidence: ReattachEvidence | None = None,
    ) -> RuntimeExecution:
        if claim_reason not in {"initial", "reattach", "replacement"}:
            raise InvalidTaskInput("Runtime claim reason is invalid")
        if reattach_evidence is not None:
            inspected = reattach_evidence.inspected_at
            current_time = now.astimezone(timezone.utc)
            if (
                inspected.tzinfo is None
                or inspected > current_time
                or current_time - inspected > timedelta(minutes=5)
            ):
                raise InvalidTaskTransition("Runtime reattach evidence is stale")
        statement = (
            select(RuntimeExecutionRecord)
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeExecutionRecord.id == execution_id,
                RuntimeExecutionRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        record = self._session.scalar(statement)
        execution = _execution_domain(record)
        if execution is None:
            raise LookupError(execution_id)
        if reattach_evidence is not None:
            version = self.get_version(
                execution.runtime_version_id, tenant_id=tenant_id, for_update=False
            )
            if version is None:
                raise InvalidTaskTransition("Runtime Version is unavailable")
            try:
                descriptor = RuntimeDescriptor.from_dict(dict(version.descriptor))
            except Exception as exc:
                raise InvalidTaskTransition("Runtime Version descriptor is invalid") from exc
            if not descriptor.capabilities.reattach:
                raise InvalidTaskTransition("Runtime Version does not permit reattach")
        new_attempt = self._session.scalar(
            select(TaskAttemptRecord).where(TaskAttemptRecord.id == attempt_id).with_for_update()
        )
        if new_attempt is None or new_attempt.run_id != execution.run_id:
            raise InvalidTaskTransition("Runtime owner Attempt is not bound to the Run")
        if (
            new_attempt.status != "RUNNING"
            or new_attempt.fencing_token != fencing_token
            or new_attempt.lease_expires_at <= now
        ):
            raise InvalidTaskTransition("Runtime owner Attempt lease is not active")
        old_attempt = None
        replacement_authorized = execution.current_owner_attempt_id is None
        if execution.current_owner_attempt_id is not None:
            old_attempt = self._session.scalar(
                select(TaskAttemptRecord)
                .where(TaskAttemptRecord.id == execution.current_owner_attempt_id)
                .with_for_update()
            )
            if old_attempt is None or old_attempt.run_id != execution.run_id:
                raise InvalidTaskTransition("Runtime owner history is inconsistent")
            replacement_authorized = (
                old_attempt.status != "RUNNING" or old_attempt.lease_expires_at <= now
            )
        if (
            execution.current_fencing_token is not None
            and fencing_token <= execution.current_fencing_token
        ):
            raise InvalidTaskInput("Runtime fencing token must increase")
        updated = execution.claim(
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            expected_owner_attempt_id=expected_owner_attempt_id,
            expected_fencing_token=expected_fencing_token,
            expected_version=expected_version,
            now=now,
            replacement_authorized=replacement_authorized,
            reattach_evidence=reattach_evidence,
        )
        if updated is execution:
            return execution
        assert record is not None
        for key, value in _execution_values(updated).items():
            if key != "id":
                setattr(record, key, value)
        if execution.current_owner_attempt_id is not None:
            previous_history = self._session.scalar(
                select(RuntimeOwnershipHistoryRecord)
                .where(
                    RuntimeOwnershipHistoryRecord.runtime_execution_id == execution_id,
                    RuntimeOwnershipHistoryRecord.fencing_token
                    == execution.current_fencing_token,
                )
                .with_for_update()
            )
            if previous_history is not None:
                previous_history.released_at = now
                previous_history.release_reason = claim_reason
        self._session.add(
            RuntimeOwnershipHistoryRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                runtime_execution_id=execution_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                previous_attempt_id=execution.current_owner_attempt_id,
                claimed_at=now,
                released_at=None,
                release_reason=None,
                claim_reason=claim_reason[:64],
            )
        )
        return updated

    def add_ownership_history(self, **values: Any) -> None:
        self._session.add(RuntimeOwnershipHistoryRecord(**values))

    def add_observation(self, **values: Any) -> RuntimeObservationRecord:
        record = RuntimeObservationRecord(**values)
        self._session.add(record)
        return record

    def find_observations(
        self, execution_id: UUID, *, tenant_id: str, limit: int, offset: int
    ) -> list[RuntimeObservationRecord]:
        statement = (
            select(RuntimeObservationRecord)
            .join(
                RuntimeExecutionRecord,
                RuntimeExecutionRecord.id == RuntimeObservationRecord.runtime_execution_id,
            )
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeObservationRecord.runtime_execution_id == execution_id,
                RuntimeObservationRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
            )
            .order_by(RuntimeObservationRecord.received_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement))

    def prior_observations(
        self, execution_id: UUID, *, tenant_id: str, observation_id: str, digest: str
    ) -> list[RuntimeObservationRecord]:
        statement = (
            select(RuntimeObservationRecord)
            .join(
                RuntimeExecutionRecord,
                RuntimeExecutionRecord.id == RuntimeObservationRecord.runtime_execution_id,
            )
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeObservationRecord.runtime_execution_id == execution_id,
                RuntimeObservationRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
                (RuntimeObservationRecord.observation_id == observation_id)
                | (RuntimeObservationRecord.observation_digest == digest),
            )
        )
        return list(self._session.scalars(statement))

    def add_lifecycle_operation(self, **values: Any) -> None:
        self._session.add(RuntimeLifecycleOperationRecord(**values))

    def find_lifecycle_operation(
        self, execution_id: UUID, *, tenant_id: str, operation_id: str
    ) -> RuntimeLifecycleOperationRecord | None:
        return self._session.scalar(
            select(RuntimeLifecycleOperationRecord)
            .join(
                RuntimeExecutionRecord,
                RuntimeExecutionRecord.id == RuntimeLifecycleOperationRecord.runtime_execution_id,
            )
            .join(TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                RuntimeLifecycleOperationRecord.runtime_execution_id == execution_id,
                RuntimeLifecycleOperationRecord.tenant_id == tenant_id,
                TaskRecord.tenant_id == tenant_id,
                RuntimeLifecycleOperationRecord.operation_id == operation_id,
            )
        )

    @staticmethod
    def _scope(model: Any, *, tenant_id: str, principal_id: UUID | None) -> Any:
        return (model.visibility == RuntimeVisibility.PLATFORM.value) | (
            (model.tenant_id == tenant_id)
            & (
                (model.visibility == RuntimeVisibility.TENANT.value)
                | (
                    (model.visibility == RuntimeVisibility.PRIVATE.value)
                    & (model.owner_principal_id == principal_id)
                )
            )
        )


def _registration_values(value: RuntimeRegistration) -> dict[str, Any]:
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "name": value.name,
        "owner_principal_id": value.owner_principal_id,
        "visibility": value.visibility.value,
        "status": value.status.value,
        "default_version_id": value.default_version_id,
        "version": value.version,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _execution_values(value: RuntimeExecution) -> dict[str, Any]:
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "run_id": value.run_id,
        "runtime_version_id": value.runtime_version_id,
        "assignment_id": value.assignment_id,
        "assignment_digest": value.assignment_digest,
        "dispatch_key": value.dispatch_key,
        "dispatch_digest": value.dispatch_digest,
        "provider_execution_ref": value.provider_execution_ref,
        "provider_generation": value.provider_generation,
        "phase": value.phase.value,
        "current_owner_attempt_id": value.current_owner_attempt_id,
        "current_fencing_token": value.current_fencing_token,
        "provider_sequence": value.provider_sequence,
        "checkpoint_ref": value.checkpoint_ref,
        "workspace_ref": value.workspace_ref,
        "version": value.version,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "terminal_at": value.terminal_at,
    }


def _registration_domain(record: RuntimeRegistrationRecord | None) -> RuntimeRegistration | None:
    if record is None:
        return None
    return RuntimeRegistration(
        id=record.id,
        tenant_id=record.tenant_id,
        name=record.name,
        owner_principal_id=record.owner_principal_id,
        visibility=RuntimeVisibility(record.visibility),
        status=RuntimeRegistrationStatus(record.status),
        default_version_id=record.default_version_id,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _version_domain(record: RuntimeVersionRecord | None) -> RuntimeVersion | None:
    if record is None:
        return None
    return RuntimeVersion(
        id=record.id,
        runtime_id=record.runtime_id,
        api_version=record.api_version,
        adapter_kind=record.adapter_kind,
        artifact_digest=record.artifact_digest,
        configuration_digest=record.configuration_digest,
        descriptor=dict(record.descriptor),
        trust_profile=RuntimeTrustProfile(record.trust_profile),
        compatibility=dict(record.compatibility),
        status=RuntimeVersionStatus(record.status),
        created_at=record.created_at,
        published_at=record.published_at,
        revoked_at=record.revoked_at,
    )


def _execution_domain(record: RuntimeExecutionRecord | None) -> RuntimeExecution | None:
    if record is None:
        return None
    return RuntimeExecution(
        id=record.id,
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        runtime_version_id=record.runtime_version_id,
        assignment_id=record.assignment_id,
        assignment_digest=record.assignment_digest,
        dispatch_key=record.dispatch_key,
        dispatch_digest=record.dispatch_digest,
        provider_execution_ref=record.provider_execution_ref,
        provider_generation=record.provider_generation,
        phase=RuntimeExecutionPhase(record.phase),
        current_owner_attempt_id=record.current_owner_attempt_id,
        current_fencing_token=record.current_fencing_token,
        provider_sequence=record.provider_sequence,
        checkpoint_ref=record.checkpoint_ref,
        workspace_ref=record.workspace_ref,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        terminal_at=record.terminal_at,
    )
