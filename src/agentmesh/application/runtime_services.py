"""Application commands and read projections for managed runtime persistence.

No command in this module calls a provider.  The UoW only records intent/evidence;
dispatch is an A2 responsibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.ports import UnitOfWork
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeExecutionNotFound,
    RuntimeNotFound,
    RuntimeRegistryConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
    RuntimeRegistration,
    RuntimeRegistrationStatus,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
    RuntimeVisibility,
)
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import canonical_digest, canonical_json_bytes
from agentmesh.runtime_sdk.builtin import langgraph_descriptor, langgraph_v2_descriptor
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeRegistryService:
    def __init__(
        self,
        *,
        uow_factory: Any,
        tenant_id: str,
        principal_id: UUID | None = None,
        feature_gates: FeatureGateSet | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._feature_gates = feature_gates

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def _require_enabled(self) -> None:
        if self._feature_gates is not None:
            self._feature_gates.require(Feature.MANAGED_AGENT_RUNTIME)

    def list_registrations(
        self, *, limit: int = 50, offset: int = 0, principal_id: UUID | None = None
    ) -> list[RuntimeRegistration]:
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.runtimes.list_registrations(
                tenant_id=self._tenant_id,
                principal_id=principal_id or self._principal_id,
                limit=limit,
                offset=offset,
            )

    def get_registration(
        self, registration_id: UUID, *, principal_id: UUID | None = None
    ) -> RuntimeRegistration:
        self._require_enabled()
        with self._uow_factory() as uow:
            value = uow.runtimes.get_registration(
                registration_id,
                tenant_id=self._tenant_id,
                principal_id=principal_id or self._principal_id,
            )
        if value is None:
            raise RuntimeNotFound("Runtime registration was not found")
        return value

    def list_versions(
        self, runtime_id: UUID, *, principal_id: UUID | None = None
    ) -> list[RuntimeVersion]:
        self._require_enabled()
        with self._uow_factory() as uow:
            if (
                uow.runtimes.get_registration(
                    runtime_id,
                    tenant_id=self._tenant_id,
                    principal_id=principal_id or self._principal_id,
                )
                is None
            ):
                raise RuntimeNotFound("Runtime registration was not found")
            return uow.runtimes.list_versions(
                runtime_id,
                tenant_id=self._tenant_id,
                principal_id=principal_id or self._principal_id,
            )

    def get_execution(self, execution_id: UUID) -> RuntimeExecution:
        self._require_enabled()
        with self._uow_factory() as uow:
            value = uow.runtimes.get_execution(execution_id, tenant_id=self._tenant_id)
        if value is None:
            raise RuntimeExecutionNotFound("Runtime execution was not found")
        return value

    def list_executions(self, *, limit: int = 50, offset: int = 0) -> list[RuntimeExecution]:
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.runtimes.list_executions_for_tenant(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def list_observations(
        self, execution_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        self._require_enabled()
        # The repository validates execution tenant through Task -> Run before returning evidence.
        with self._uow_factory() as uow:
            if uow.runtimes.get_execution(execution_id, tenant_id=self._tenant_id) is None:
                raise RuntimeExecutionNotFound("Runtime execution was not found")
            records = uow.runtimes.find_observations(
                execution_id, tenant_id=self._tenant_id, limit=limit, offset=offset
            )
            return [
                {
                    "id": record.id,
                    "observation_id": record.observation_id,
                    "observation_digest": record.observation_digest,
                    "assignment_id": record.assignment_id,
                    "assignment_digest": record.assignment_digest,
                    "provider_sequence": record.provider_sequence,
                    "phase": record.phase,
                    "observed_at": record.observed_at,
                    "received_at": record.received_at,
                    "safe_summary": record.safe_summary,
                    "processing_outcome": record.processing_outcome,
                    "provider_event_present": record.provider_event_present,
                }
                for record in records
            ]

    def create_registration(
        self,
        *,
        name: str,
        owner_principal_id: UUID,
        visibility: RuntimeVisibility,
        tenant_id: str | None = None,
    ) -> RuntimeRegistration:
        self._require_enabled()
        with self._uow_factory() as uow:
            if uow.runtimes.get_registration_by_name(
                name, tenant_id=self._tenant_id, principal_id=self._principal_id, for_update=True
            ):
                raise RuntimeRegistryConflict("Runtime registration identity already exists")
            value = RuntimeRegistration.create(
                name=name,
                owner_principal_id=owner_principal_id,
                visibility=visibility,
                tenant_id=tenant_id,
            )
            uow.runtimes.add_registration(value)
            uow.commit()
            return value

    def ensure_builtin_langgraph(self, *, owner_principal_id: UUID) -> RuntimeVersion:
        """Publish immutable v1 compatibility and honest deterministic v2."""
        runtime_id = uuid5(NAMESPACE_URL, "agentmesh:runtime:langgraph")
        versions = (
            ("v1", langgraph_descriptor()),
            ("v2", langgraph_v2_descriptor()),
        )
        with self._uow_factory() as uow:
            registration = uow.runtimes.get_registration(
                runtime_id, tenant_id=self._tenant_id, principal_id=owner_principal_id
            )
            if registration is None:
                registration = RuntimeRegistration.create(
                    registration_id=runtime_id,
                    name="langgraph",
                    owner_principal_id=owner_principal_id,
                    visibility=RuntimeVisibility.PLATFORM,
                )
                uow.runtimes.add_registration(registration)
            published: dict[str, RuntimeVersion] = {}
            for release, descriptor in versions:
                version_id = uuid5(
                    NAMESPACE_URL, f"agentmesh:runtime:langgraph:{release}"
                )
                version = uow.runtimes.get_version(
                    version_id, tenant_id=self._tenant_id, principal_id=owner_principal_id
                )
                if version is None:
                    artifact_digest = canonical_digest(
                        {
                            "package": "agentmesh",
                            "runtime": "agentmesh.langgraph",
                            "release": release,
                        }
                    )
                    configuration_digest = canonical_digest(
                        {
                            "runtime_key": descriptor["runtime_key"],
                            "capabilities": descriptor["capabilities"],
                            "limits": descriptor["limits"],
                        }
                    )
                    RuntimeDescriptor.from_dict(descriptor)
                    version = RuntimeVersion(
                        id=version_id,
                        runtime_id=runtime_id,
                        api_version=1,
                        adapter_kind="python-in-process",
                        artifact_digest=artifact_digest,
                        configuration_digest=configuration_digest,
                        descriptor=descriptor,
                        trust_profile=RuntimeTrustProfile.BUILT_IN,
                        compatibility={},
                        status=RuntimeVersionStatus.DRAFT,
                        created_at=_now(),
                        published_at=None,
                    )
                    uow.runtimes.add_version(version)
                    version = version.publish()
                    uow.runtimes.save_version(
                        version,
                        tenant_id=self._tenant_id,
                        principal_id=owner_principal_id,
                    )
                published[release] = version
            version = published["v2"]
            if registration.default_version_id != version.id:
                uow.runtimes.save_registration(
                    registration.set_default(version),
                    tenant_id=self._tenant_id,
                    principal_id=owner_principal_id,
                )
            uow.commit()
            return version

    def publish_version(self, version: RuntimeVersion) -> RuntimeVersion:
        self._require_enabled()
        RuntimeDescriptor.from_dict(dict(version.descriptor))
        with self._uow_factory() as uow:
            registration = uow.runtimes.get_registration(
                version.runtime_id, tenant_id=self._tenant_id, for_update=True
            )
            if registration is None:
                raise RuntimeNotFound("Runtime registration was not found")
            current = uow.runtimes.get_version(
                version.id, tenant_id=self._tenant_id, for_update=True
            )
            if current is None:
                uow.runtimes.add_version(version)
                current = version
            if current.status is RuntimeVersionStatus.DRAFT:
                current = current.publish()
                uow.runtimes.save_version(
                    current, tenant_id=self._tenant_id, principal_id=self._principal_id
                )
            elif current.status is not RuntimeVersionStatus.PUBLISHED:
                raise RuntimeRegistryConflict("Runtime Version is not publishable")
            updated = registration.set_default(current)
            uow.runtimes.save_registration(
                updated, tenant_id=self._tenant_id, principal_id=self._principal_id
            )
            uow.commit()
            return current

    def prepare_execution(
        self,
        *,
        run_id: UUID,
        assignment_id: UUID,
        assignment_digest: str,
        dispatch_key: str | None = None,
        execution_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        self._require_enabled()
        with self._uow_factory() as uow:
            value = self.prepare_execution_in_uow(
                uow,
                run_id=run_id,
                assignment_id=assignment_id,
                assignment_digest=assignment_digest,
                dispatch_key=dispatch_key,
                execution_id=execution_id,
                now=now,
            )
            uow.commit()
            return value

    def prepare_execution_in_uow(
        self,
        uow: UnitOfWork,
        *,
        run_id: UUID,
        assignment_id: UUID,
        assignment_digest: str,
        dispatch_key: str | None = None,
        execution_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        """Prepare and bind execution on a caller-owned transaction.

        Task admission uses this boundary so the Run pin, Runtime execution,
        and RunRequested outbox event become visible atomically.
        """
        self._require_enabled()
        timestamp = now or _now()
        run = uow.runs.get(run_id, for_update=True)
        if run is None:
            raise RuntimeExecutionNotFound("Task Run was not found")
        task = uow.tasks.get(run.task_id)
        if task is None or task.tenant_id != self._tenant_id:
            raise RuntimeExecutionNotFound("Task Run was not found")
        if run.runtime_version_id is None:
            raise RuntimeVersionNotFound("Task Run has no pinned Runtime Version")
        version = uow.runtimes.get_version(
            run.runtime_version_id, tenant_id=self._tenant_id, for_update=True
        )
        if version is None or version.status is not RuntimeVersionStatus.PUBLISHED:
            raise RuntimeVersionNotFound("Pinned Runtime Version is unavailable")
        registration = uow.runtimes.get_registration(
            version.runtime_id, tenant_id=self._tenant_id, for_update=True
        )
        if registration is None or registration.status is not RuntimeRegistrationStatus.ACTIVE:
            raise RuntimeRegistryConflict("Runtime registration is not active")
        existing = uow.runtimes.get_active_or_unresolved_for_run(
            run_id, tenant_id=self._tenant_id, for_update=True
        )
        if existing is not None:
            if existing.phase is RuntimeExecutionPhase.OUTCOME_UNKNOWN:
                raise RuntimeExecutionConflict("Unknown Runtime outcome requires reconciliation")
            if (
                existing.assignment_id != assignment_id
                or existing.assignment_digest != assignment_digest
            ):
                raise RuntimeExecutionConflict("Run already has a different Runtime assignment")
            if execution_id is not None and existing.id != execution_id:
                raise RuntimeExecutionConflict("Run is already bound to another Runtime execution")
            expected_key = f"runtime-dispatch:{self._tenant_id}:{existing.id}"
            if dispatch_key is not None and dispatch_key != expected_key:
                raise RuntimeExecutionConflict("Runtime dispatch key is not bound to execution")
            return existing
        if run.runtime_execution_id is not None:
            if execution_id is not None and execution_id != run.runtime_execution_id:
                raise RuntimeExecutionConflict("Run Runtime execution identity is immutable")
            resolved_execution_id = run.runtime_execution_id
        elif run.runtime_execution_intent_id is not None:
            if execution_id is not None and execution_id != run.runtime_execution_intent_id:
                raise RuntimeExecutionConflict("Run Runtime execution identity is immutable")
            resolved_execution_id = run.runtime_execution_intent_id
        else:
            resolved_execution_id = execution_id or uuid4()
        stable_key = f"runtime-dispatch:{self._tenant_id}:{resolved_execution_id}"
        if dispatch_key is not None and dispatch_key != stable_key:
            raise RuntimeExecutionConflict("Runtime dispatch key is not bound to execution")
        if len(stable_key) > 512:
            raise InvalidTaskTransition("Runtime dispatch key is invalid")
        stable_digest = canonical_digest(
            {
                "execution_id": str(resolved_execution_id),
                "dispatch_key": stable_key,
                "assignment_digest": assignment_digest,
            }
        )
        value = RuntimeExecution.prepare(
            tenant_id=self._tenant_id,
            run_id=run_id,
            runtime_version_id=version.id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
            dispatch_key=stable_key,
            dispatch_digest=stable_digest,
            execution_id=resolved_execution_id,
            now=timestamp,
        )
        by_key = uow.runtimes.get_execution_by_dispatch(
            stable_key, tenant_id=self._tenant_id, for_update=True
        )
        if by_key is not None:
            if by_key.dispatch_digest != stable_digest:
                raise RuntimeExecutionConflict("Dispatch key has a different digest")
            return by_key
        uow.runtimes.add_execution(value)
        run.bind_runtime_execution(value.id)
        uow.runs.save(run)
        return value

    def admit_deterministic_shadow(
        self,
        *,
        run_id: UUID,
        assignment_id: UUID,
        assignment_digest: str,
        dispatch_key: str | None = None,
        execution_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        """Pin a queued Run for A2 shadow comparison while legacy stays authoritative."""
        execution = self.prepare_execution(
            run_id=run_id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
            dispatch_key=dispatch_key,
            execution_id=execution_id,
            now=now,
        )
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id, for_update=True)
            if run is None or run.runtime_execution_id != execution.id:
                raise RuntimeExecutionNotFound("Task Run Runtime binding was not persisted")
            if run.comparison_mode != "deterministic_shadow":
                raise RuntimeExecutionConflict("Run was not admitted for deterministic comparison")
            uow.runs.save(run)
            uow.commit()
        return execution

    def record_observation(
        self,
        *,
        execution_id: UUID,
        observation_id: str,
        observation_digest: str,
        assignment_id: UUID,
        assignment_digest: str,
        phase: RuntimeExecutionPhase,
        provider_sequence: int | None,
        observed_at: datetime,
        evidence: dict[str, Any] | None = None,
        safe_summary: str | None = None,
        attempt_id: UUID | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> RuntimeObservationOutcome:
        self._require_enabled()
        timestamp = now or _now()
        evidence = {} if evidence is None else evidence
        if (
            type(observation_id) is not str
            or not observation_id.strip()
            or len(observation_id) > 512
            or type(observation_digest) is not str
            or type(assignment_id) is not UUID
            or type(assignment_digest) is not str
            or type(phase) is not RuntimeExecutionPhase
            or type(provider_sequence) not in (int, type(None))
            or (provider_sequence is not None and provider_sequence < 0)
            or type(observed_at) is not datetime
            or type(evidence) is not dict
            or (safe_summary is not None and type(safe_summary) is not str)
        ):
            raise InvalidTaskInput("Runtime observation evidence is invalid")
        try:
            evidence_bytes = canonical_json_bytes(evidence)
        except Exception as exc:
            raise InvalidTaskInput("Runtime observation evidence is invalid") from exc
        if len(evidence_bytes) > 65_536 or (
            safe_summary is not None and len(safe_summary) > 4096
        ):
            raise InvalidTaskInput("Runtime observation evidence is invalid")
        with self._uow_factory() as uow:
            execution = uow.runtimes.get_execution(
                execution_id, tenant_id=self._tenant_id, for_update=True
            )
            if execution is None:
                raise RuntimeExecutionNotFound("Runtime execution was not found")
            prior = uow.runtimes.prior_observations(
                execution_id,
                tenant_id=self._tenant_id,
                observation_id=observation_id,
                digest=observation_digest,
            )
            if (
                assignment_id != execution.assignment_id
                or assignment_digest != execution.assignment_digest
            ):
                outcome = RuntimeObservationOutcome.CONFLICT
            elif any(
                item.observation_id == observation_id
                and item.observation_digest != observation_digest
                for item in prior
            ):
                outcome = RuntimeObservationOutcome.CONFLICT
            elif prior or (
                provider_sequence is not None
                and execution.provider_sequence is not None
                and provider_sequence <= execution.provider_sequence
            ):
                outcome = RuntimeObservationOutcome.DUPLICATE
            elif (
                execution.current_owner_attempt_id is None
                or execution.current_fencing_token is None
                or execution.current_owner_attempt_id != attempt_id
                or execution.current_fencing_token != fencing_token
            ):
                outcome = RuntimeObservationOutcome.STALE_OWNER
            elif (
                provider_sequence is not None
                and execution.provider_sequence is not None
                and provider_sequence > execution.provider_sequence + 1
            ):
                outcome = RuntimeObservationOutcome.GAP
            else:
                outcome = RuntimeObservationOutcome.APPLIED
            observation_record = RuntimeObservationEvidence(
                id=uuid4(),
                tenant_id=self._tenant_id,
                runtime_execution_id=execution_id,
                observation_id=observation_id,
                observation_digest=observation_digest,
                assignment_id=assignment_id,
                assignment_digest=assignment_digest,
                provider_sequence=provider_sequence,
                phase=phase,
                observed_at=observed_at,
                received_at=timestamp,
                safe_summary=safe_summary,
                processing_outcome=outcome,
                provider_event_present=False,
                evidence=evidence,
            )
            uow.runtimes.add_observation(observation_record)
            if outcome is RuntimeObservationOutcome.APPLIED:
                try:
                    updated = execution.apply_observation(
                        phase=phase, provider_sequence=provider_sequence, now=timestamp
                    )
                except InvalidTaskTransition:
                    uow.runtimes.update_observation_outcome(
                        observation_record, outcome=RuntimeObservationOutcome.CONFLICT
                    )
                    outcome = RuntimeObservationOutcome.CONFLICT
                else:
                    uow.runtimes.save_execution(updated, tenant_id=self._tenant_id)
            uow.commit()
            return outcome

    def claim_execution_owner(
        self,
        *,
        execution_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        expected_owner_attempt_id: UUID | None,
        expected_fencing_token: int | None,
        expected_version: int,
        claim_reason: str = "initial",
        reattach_evidence: ReattachEvidence | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        self._require_enabled()
        with self._uow_factory() as uow:
            updated = uow.runtimes.claim_execution_owner(
                execution_id=execution_id,
                tenant_id=self._tenant_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                expected_owner_attempt_id=expected_owner_attempt_id,
                expected_fencing_token=expected_fencing_token,
                expected_version=expected_version,
                now=now or _now(),
                claim_reason=claim_reason,
                reattach_evidence=reattach_evidence,
            )
            uow.commit()
            return updated

    def request_lifecycle_operation(
        self,
        *,
        execution_id: UUID,
        operation_id: str,
        operation: RuntimeLifecycleOperation,
        deadline: datetime,
        intent: dict[str, Any],
        now: datetime | None = None,
    ) -> RuntimeLifecycleStatus:
        self._require_enabled()
        timestamp = now or _now()
        if (
            type(operation_id) is not str
            or not operation_id.strip()
            or len(operation_id) > 512
            or type(operation) is not RuntimeLifecycleOperation
            or type(deadline) is not datetime
            or type(intent) is not dict
        ):
            raise InvalidTaskInput("Runtime lifecycle operation identity is invalid")
        if deadline <= timestamp:
            raise InvalidTaskInput("Runtime lifecycle deadline is invalid")
        try:
            intent_bytes = canonical_json_bytes(intent)
        except Exception as exc:
            raise InvalidTaskInput("Runtime lifecycle intent is invalid") from exc
        if len(intent_bytes) > 65_536:
            raise InvalidTaskInput("Runtime lifecycle intent is invalid")
        digest = canonical_digest(intent)
        with self._uow_factory() as uow:
            execution = uow.runtimes.get_execution(
                execution_id, tenant_id=self._tenant_id, for_update=True
            )
            if execution is None:
                raise RuntimeExecutionNotFound("Runtime execution was not found")
            existing = uow.runtimes.find_lifecycle_operation(
                execution_id, tenant_id=self._tenant_id, operation_id=operation_id
            )
            if existing is not None:
                if existing.intent_digest != digest:
                    raise RuntimeExecutionConflict("Lifecycle operation identity conflicts")
                return RuntimeLifecycleStatus(existing.status)
            lifecycle = RuntimeLifecycleIntent(
                id=uuid4(),
                tenant_id=self._tenant_id,
                runtime_execution_id=execution_id,
                operation_id=operation_id,
                operation=operation,
                intent_digest=digest,
                status=RuntimeLifecycleStatus.REQUESTED,
                deadline=deadline,
                receipt_summary=None,
                version=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
            uow.runtimes.add_lifecycle_operation(lifecycle)
            requested_phase = {
                RuntimeLifecycleOperation.PAUSE: RuntimeExecutionPhase.PAUSE_REQUESTED,
                RuntimeLifecycleOperation.CANCEL: RuntimeExecutionPhase.CANCEL_REQUESTED,
            }.get(operation)
            if requested_phase is not None:
                try:
                    updated = execution.apply_observation(
                        phase=requested_phase,
                        provider_sequence=execution.provider_sequence,
                        now=timestamp,
                    )
                except InvalidTaskTransition:
                    # Preserve the immutable intent, but report that it cannot
                    # be applied to this phase.  No provider is contacted.
                    uow.runtimes.update_lifecycle_status(
                        lifecycle,
                        status=RuntimeLifecycleStatus.REJECTED,
                        now=timestamp,
                    )
                    uow.commit()
                    return RuntimeLifecycleStatus.REJECTED
                else:
                    uow.runtimes.save_execution(updated, tenant_id=self._tenant_id)
            uow.commit()
            return RuntimeLifecycleStatus.REQUESTED
