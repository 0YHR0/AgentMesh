"""Application commands and read projections for managed runtime persistence.

No command in this module calls a provider.  The UoW only records intent/evidence;
dispatch is an A2 responsibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.ports import (
    LateTerminalObservationResult,
    LateTerminalObservationResultKind,
    ManagedRuntimeConflictObservation,
    UnitOfWork,
)
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    RuntimeHandleSnapshot,
    assignment_snapshot_for,
    handle_snapshot_for,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeExecutionNotFound,
    RuntimeNotFound,
    RuntimeRegistryConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentStatus,
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
from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimeExecutionHandle,
    RuntimeObservation,
    canonical_digest,
    canonical_json_bytes,
)
from agentmesh.runtime_sdk.builtin import (
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
    langgraph_descriptor,
    langgraph_v2_descriptor,
)
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_digest_identity(value: str | None) -> str | None:
    """Return the digest identity shared by SDK and persisted legacy rows."""
    if value is None:
        return None
    return value.strip().lower().removeprefix("sha256:")


def _conflicting_evidence_matches(
    existing: RuntimeObservationEvidence,
    *,
    observation: ManagedRuntimeConflictObservation,
    tenant_id: str,
    execution_id: UUID,
    phase: RuntimeExecutionPhase,
    assignment_id: UUID,
    assignment_digest: str,
    evidence_flags: dict[str, bool],
) -> bool:
    return (
        existing.tenant_id == tenant_id
        and existing.runtime_execution_id == execution_id
        and existing.observation_id == str(observation.observation_id)
        and existing.observation_digest == observation.observation_digest
        and existing.assignment_id == assignment_id
        and existing.assignment_digest == assignment_digest
        and existing.provider_sequence == observation.provider_sequence
        and existing.phase is phase
        and existing.observed_at == observation.observed_at
        and existing.processing_outcome is RuntimeObservationOutcome.CONFLICT
        and existing.provider_event_present is False
        and existing.safe_summary == "Managed Runtime terminal contract conflict"
        and dict(existing.evidence) == evidence_flags
    )


def _late_terminal_evidence_matches(
    existing: RuntimeObservationEvidence,
    *,
    tenant_id: str,
    execution: RuntimeExecution,
    candidate_phase: RuntimeExecutionPhase,
    observation: RuntimeObservation,
    candidate_digest: str,
) -> bool:
    return (
        existing.tenant_id == tenant_id
        and existing.runtime_execution_id == execution.id
        and existing.observation_id
        == str(uuid5(NAMESPACE_URL, f"{execution.id}:{candidate_digest}"))
        and existing.observation_digest == candidate_digest
        and existing.assignment_id == execution.assignment_id
        and existing.assignment_digest == execution.assignment_digest
        and existing.provider_sequence == observation.provider_sequence
        and existing.phase is candidate_phase
        and existing.observed_at == observation.observed_at.astimezone(timezone.utc)
        and existing.safe_summary == "Managed Runtime late terminal conflict"
        and existing.processing_outcome is RuntimeObservationOutcome.CONFLICT
        and existing.provider_event_present is False
        and dict(existing.evidence)
        == {
            "execution_id_mismatch": False,
            "assignment_id_mismatch": False,
            "assignment_digest_mismatch": False,
            "structural_invalid": False,
            "terminal_contract_invalid": False,
            "protocol_error_observation": False,
        }
    )


def _late_terminal_incident_matches(
    current: RuntimeIntegrityIncident,
    candidate: RuntimeIntegrityIncident,
) -> bool:
    return (
        current.id == candidate.id
        and current.tenant_id == candidate.tenant_id
        and current.runtime_execution_id == candidate.runtime_execution_id
        and current.accepted_observation_id == candidate.accepted_observation_id
        and current.accepted_observation_digest == candidate.accepted_observation_digest
        and current.accepted_phase is candidate.accepted_phase
        and current.conflicting_observation_id == candidate.conflicting_observation_id
        and current.conflicting_observation_digest == candidate.conflicting_observation_digest
        and current.conflicting_phase is candidate.conflicting_phase
        and current.reason == candidate.reason
        and current.created_at == candidate.created_at
        # ``updated_at`` is operator-mutable; the creation timestamp is the
        # immutable command-time fact that must remain stable on replay.
    )


def _validate_assignment_chain(
    assignment: RuntimeAssignment,
    *,
    tenant_id: str,
    task_id: UUID,
    run: Any,
    execution_id: UUID,
) -> None:
    """Check every persisted identity reached by an Assignment snapshot."""
    try:
        assignment_task_id = UUID(assignment.task_id)
        assignment_run_id = UUID(assignment.run_id)
        assignment_runtime_version_id = UUID(assignment.runtime_version_id)
        assignment_execution_id = UUID(
            assignment.correlation_ids.get("runtime_execution_id", "")
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTaskInput("Runtime Assignment identity chain is invalid") from exc
    if (
        assignment.tenant_id != tenant_id
        or assignment_task_id != task_id
        or assignment_run_id != run.id
        or assignment_runtime_version_id != run.runtime_version_id
        or assignment_execution_id != execution_id
        or UUID(assignment.agent_version_id) != run.agent_version_id
        or _canonical_digest_identity(assignment.agent_version_digest)
        != _canonical_digest_identity(run.agent_version_digest)
    ):
        raise RuntimeExecutionConflict("Runtime Assignment identity chain conflicts")


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
        runtime_id = builtin_langgraph_runtime_id()
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
                version_id = builtin_langgraph_version_id(release)
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

    def require_builtin_langgraph_v2_in_uow(self, uow: UnitOfWork) -> RuntimeVersion:
        """Return the published platform v2 only when bootstrap state is valid."""
        self._require_enabled()
        registration = uow.runtimes.get_registration(
            builtin_langgraph_runtime_id(), tenant_id=self._tenant_id
        )
        if (
            registration is None
            or registration.status is not RuntimeRegistrationStatus.ACTIVE
            or registration.default_version_id != builtin_langgraph_version_id("v2")
        ):
            raise RuntimeRegistryConflict(
                "Built-in LangGraph v2 is not the active published Runtime default"
            )
        version = uow.runtimes.get_version(
            builtin_langgraph_version_id("v2"), tenant_id=self._tenant_id
        )
        if (
            version is None
            or version.runtime_id != registration.id
            or version.status is not RuntimeVersionStatus.PUBLISHED
            or version.descriptor.get("runtime_key") != "agentmesh.langgraph"
        ):
            raise RuntimeVersionNotFound("Built-in LangGraph v2 is unavailable")
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

    def get_assignment_snapshot(self, execution_id: UUID) -> RuntimeAssignmentSnapshot | None:
        """Read the immutable Assignment bytes for replacement/reattach."""
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.runtimes.get_assignment_snapshot(
                execution_id, tenant_id=self._tenant_id
            )

    def get_handle_snapshot(self, execution_id: UUID) -> RuntimeHandleSnapshot | None:
        """Read the immutable provider handle for lifecycle reconstruction."""
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.runtimes.get_handle_snapshot(execution_id, tenant_id=self._tenant_id)

    def prepare_execution_with_assignment_snapshot(
        self,
        *,
        run_id: UUID,
        assignment: RuntimeAssignment,
        dispatch_key: str | None = None,
        execution_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        """Atomically prepare/bind an execution and persist its exact Assignment.

        The transaction contains only control-plane rows.  It is committed
        before the caller invokes any adapter, so provider calls never execute
        while this UoW holds locks.
        """
        self._require_enabled()
        timestamp = now or _now()
        try:
            requested_execution_id = execution_id or UUID(
                assignment.correlation_ids.get("runtime_execution_id", "")
            )
        except (TypeError, ValueError) as exc:
            raise InvalidTaskInput("Runtime Assignment execution identity is invalid") from exc
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id, for_update=True)
            if run is None:
                raise RuntimeExecutionNotFound("Task Run was not found")
            task = uow.tasks.get(run.task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise RuntimeExecutionNotFound("Task Run was not found")
            _validate_assignment_chain(
                assignment,
                tenant_id=self._tenant_id,
                task_id=task.id,
                run=run,
                execution_id=requested_execution_id,
            )
            execution = self.prepare_execution_in_uow(
                uow,
                run_id=run_id,
                assignment_id=UUID(assignment.assignment_id),
                assignment_digest=assignment.assignment_digest or "",
                dispatch_key=dispatch_key,
                execution_id=requested_execution_id,
                now=timestamp,
            )
            if execution.id != requested_execution_id:
                raise RuntimeExecutionConflict("Runtime execution identity is not canonical")
            candidate = assignment_snapshot_for(
                assignment,
                tenant_id=self._tenant_id,
                runtime_execution_id=execution.id,
                created_at=timestamp,
            )
            existing = uow.runtimes.get_assignment_snapshot(
                execution.id, tenant_id=self._tenant_id
            )
            if existing is None and execution.phase is not RuntimeExecutionPhase.PREPARED:
                raise RuntimeExecutionConflict(
                    "Runtime Assignment snapshot is missing after dispatch boundary"
                )
            uow.runtimes.add_assignment_snapshot(candidate)
            uow.commit()
            return execution

    def bind_handle_snapshot(
        self,
        *,
        handle: RuntimeExecutionHandle,
        attempt_id: UUID | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> RuntimeHandleSnapshot:
        """Persist a provider handle and safe Runtime projections atomically."""
        self._require_enabled()
        timestamp = now or _now()
        execution_id = UUID(handle.runtime_execution_id)
        with self._uow_factory() as uow:
            execution = uow.runtimes.get_execution(
                execution_id, tenant_id=self._tenant_id, for_update=True
            )
            if execution is None:
                raise RuntimeExecutionConflict("Runtime handle execution is unavailable")
            if execution.phase is RuntimeExecutionPhase.PREPARED:
                raise RuntimeExecutionConflict(
                    "Runtime handle cannot bind before dispatch boundary"
                )
            if attempt_id is not None and (
                execution.current_owner_attempt_id != attempt_id
                or execution.current_fencing_token != fencing_token
            ):
                raise RuntimeExecutionConflict("Runtime handle owner fence is stale")
            snapshot = handle_snapshot_for(
                handle,
                tenant_id=self._tenant_id,
                created_at=handle.created_at,
            )
            persisted = uow.runtimes.add_handle_snapshot(snapshot)
            updated = execution.bind_handle(
                provider_execution_ref=handle.provider_execution_ref,
                provider_generation=handle.provider_generation,
                now=timestamp,
            )
            if updated != execution:
                uow.runtimes.save_execution(updated, tenant_id=self._tenant_id)
            uow.commit()
            return persisted

    def get_execution_for_run(self, run_id: UUID) -> RuntimeExecution | None:
        """Return the active or unresolved execution for recovery decisions."""
        self._require_enabled()
        with self._uow_factory() as uow:
            return uow.runtimes.get_active_or_unresolved_for_run(
                run_id, tenant_id=self._tenant_id, for_update=False
            )

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
        with self._uow_factory() as uow:
            outcome = self.record_observation_in_uow(
                uow,
                execution_id=execution_id,
                observation_id=observation_id,
                observation_digest=observation_digest,
                assignment_id=assignment_id,
                assignment_digest=assignment_digest,
                phase=phase,
                provider_sequence=provider_sequence,
                observed_at=observed_at,
                evidence=evidence,
                safe_summary=safe_summary,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                now=now,
            )
            uow.commit()
            return outcome

    def record_conflicting_observation_in_uow(
        self,
        uow: UnitOfWork,
        *,
        execution_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        observation: ManagedRuntimeConflictObservation,
        now: datetime | None = None,
    ) -> RuntimeObservationEvidence:
        """Append safe CONFLICT evidence without mutating RuntimeExecution."""
        self._require_enabled()
        if (
            type(execution_id) is not UUID
            or type(attempt_id) is not UUID
            or type(fencing_token) is not int
            or type(observation) is not ManagedRuntimeConflictObservation
        ):
            raise InvalidTaskInput("Runtime conflict evidence identity is invalid")
        timestamp = now or _now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidTaskInput("Runtime conflict evidence timestamp is invalid")
        timestamp = timestamp.astimezone(timezone.utc)
        expected_observation_id = uuid5(
            NAMESPACE_URL, f"{execution_id}:{observation.observation_digest}"
        )
        if observation.observation_id != expected_observation_id:
            raise RuntimeExecutionConflict("Runtime conflict observation identity is invalid")
        try:
            phase = RuntimeExecutionPhase(observation.phase.value)
        except ValueError as exc:
            raise InvalidTaskInput("Runtime conflict observation phase is invalid") from exc
        execution = uow.runtimes.get_execution(
            execution_id, tenant_id=self._tenant_id, for_update=True
        )
        if execution is None:
            raise RuntimeExecutionNotFound("Runtime execution was not found")
        if (
            execution.current_owner_attempt_id != attempt_id
            or execution.current_fencing_token != fencing_token
        ):
            raise RuntimeExecutionConflict("Runtime execution owner is stale")
        evidence_flags = {
            "execution_id_mismatch": observation.execution_id_mismatch,
            "assignment_id_mismatch": observation.assignment_id_mismatch,
            "assignment_digest_mismatch": observation.assignment_digest_mismatch,
            "structural_invalid": observation.structural_invalid,
            "terminal_contract_invalid": observation.terminal_contract_invalid,
            "protocol_error_observation": observation.protocol_error_observation,
        }
        if not any(evidence_flags.values()):
            raise InvalidTaskInput("Runtime conflict evidence has no contract violation")
        prior = uow.runtimes.prior_observations(
            execution_id,
            tenant_id=self._tenant_id,
            observation_id=str(observation.observation_id),
            digest=observation.observation_digest,
        )
        for existing in prior:
            if _conflicting_evidence_matches(
                existing,
                observation=observation,
                tenant_id=self._tenant_id,
                execution_id=execution_id,
                phase=phase,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                evidence_flags=evidence_flags,
            ):
                return existing
            raise RuntimeExecutionConflict("Runtime conflict observation collides with evidence")
        if execution.phase.terminal:
            raise InvalidTaskTransition(
                "A new Runtime conflict marker cannot be added after terminal execution"
            )
        evidence = RuntimeObservationEvidence(
            id=uuid4(),
            tenant_id=self._tenant_id,
            runtime_execution_id=execution_id,
            observation_id=str(observation.observation_id),
            observation_digest=observation.observation_digest,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            provider_sequence=observation.provider_sequence,
            phase=phase,
            observed_at=observation.observed_at,
            received_at=timestamp,
            safe_summary="Managed Runtime terminal contract conflict",
            processing_outcome=RuntimeObservationOutcome.CONFLICT,
            provider_event_present=False,
            evidence=evidence_flags,
        )
        uow.runtimes.add_observation(evidence)
        return evidence

    def record_late_terminal_observation_in_uow(
        self,
        uow: UnitOfWork,
        *,
        execution_id: UUID,
        observation: RuntimeObservation,
        received_at: datetime,
    ) -> LateTerminalObservationResult:
        """Record a terminal observation received after business commit.

        This is deliberately a caller-owned transaction boundary.  The locked
        RuntimeExecution and its single accepted anchor are the only authority;
        this method never applies the candidate or rewrites any business state.
        """
        self._require_enabled()
        if type(execution_id) is not UUID or type(observation) is not RuntimeObservation:
            raise InvalidTaskInput("Late-terminal observation identity is invalid")
        if (
            type(received_at) is not datetime
            or received_at.tzinfo is None
            or received_at.utcoffset() is None
        ):
            raise InvalidTaskInput("Late-terminal receipt timestamp is invalid")
        receipt_timestamp = received_at.astimezone(timezone.utc)
        execution = uow.runtimes.get_execution(
            execution_id, tenant_id=self._tenant_id, for_update=True
        )
        if execution is None:
            raise RuntimeExecutionNotFound("Runtime execution was not found")
        accepted_phases = {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        if execution.phase not in accepted_phases:
            raise InvalidTaskTransition(
                "Late-terminal observations require a known terminal Runtime phase"
            )
        # The validator is shared with dispatch finalization.  Do not turn
        # unexpected implementation errors into provider conflict evidence.
        validate_terminal_observation(
            observation,
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            require_known_terminal=True,
        )
        try:
            candidate_phase = RuntimeExecutionPhase(observation.phase.value)
        except ValueError as exc:  # pragma: no cover - SDK enum is closed
            raise InvalidTaskInput("Late-terminal observation phase is invalid") from exc
        anchors = uow.runtimes.accepted_terminal_observations(
            execution_id,
            tenant_id=self._tenant_id,
            phase=execution.phase,
        )
        if len(anchors) != 1:
            raise RuntimeExecutionConflict(
                "Runtime execution has zero or multiple accepted terminal anchors"
            )
        anchor = anchors[0]
        if (
            anchor.tenant_id != self._tenant_id
            or anchor.runtime_execution_id != execution.id
            or anchor.assignment_id != execution.assignment_id
            or anchor.assignment_digest != execution.assignment_digest
            or anchor.phase is not execution.phase
            or anchor.processing_outcome
            not in (RuntimeObservationOutcome.APPLIED, RuntimeObservationOutcome.RECONCILED)
        ):
            raise RuntimeExecutionConflict("Runtime accepted terminal anchor is inconsistent")
        candidate_digest = canonical_digest(observation.to_dict())
        if candidate_digest == anchor.observation_digest:
            return LateTerminalObservationResult(
                kind=LateTerminalObservationResultKind.ACCEPTED_REPLAY,
                accepted_anchor=anchor,
            )

        conflict_id = uuid5(
            NAMESPACE_URL, f"{execution_id}:{candidate_digest}"
        )
        prior = uow.runtimes.prior_observations(
            execution_id,
            tenant_id=self._tenant_id,
            observation_id=str(conflict_id),
            digest=candidate_digest,
        )
        conflict_flags = {
            "execution_id_mismatch": False,
            "assignment_id_mismatch": False,
            "assignment_digest_mismatch": False,
            "structural_invalid": False,
            "terminal_contract_invalid": False,
            "protocol_error_observation": False,
        }
        conflict_evidence = next(
            (
                item
                for item in prior
                if item.observation_id == str(conflict_id)
                and item.observation_digest == candidate_digest
            ),
            None,
        )
        if conflict_evidence is None:
            if any(
                item.observation_id == str(conflict_id)
                or item.observation_digest == candidate_digest
                for item in prior
            ):
                raise RuntimeExecutionConflict(
                    "Late-terminal conflict observation collides with evidence"
                )
            conflict_evidence = RuntimeObservationEvidence(
                id=uuid4(),
                tenant_id=self._tenant_id,
                runtime_execution_id=execution_id,
                observation_id=str(conflict_id),
                observation_digest=candidate_digest,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                phase=candidate_phase,
                observed_at=observation.observed_at.astimezone(timezone.utc),
                received_at=receipt_timestamp,
                safe_summary="Managed Runtime late terminal conflict",
                processing_outcome=RuntimeObservationOutcome.CONFLICT,
                provider_event_present=False,
                evidence=conflict_flags,
            )
            uow.runtimes.add_observation(conflict_evidence)
        elif not _late_terminal_evidence_matches(
            conflict_evidence,
            tenant_id=self._tenant_id,
            execution=execution,
            candidate_phase=candidate_phase,
            observation=observation,
            candidate_digest=candidate_digest,
        ):
            raise RuntimeExecutionConflict("Late-terminal conflict evidence is inconsistent")

        incident_id = uuid5(
            NAMESPACE_URL,
            f"{self._tenant_id}:{execution_id}:{anchor.observation_digest}:{candidate_digest}",
        )
        candidate_incident = RuntimeIntegrityIncident(
            id=incident_id,
            tenant_id=self._tenant_id,
            runtime_execution_id=execution_id,
            accepted_observation_id=anchor.observation_id,
            accepted_observation_digest=anchor.observation_digest,
            accepted_phase=execution.phase,
            conflicting_observation_id=str(conflict_id),
            conflicting_observation_digest=candidate_digest,
            conflicting_phase=candidate_phase,
            status=RuntimeIntegrityIncidentStatus.OPEN,
            reason="runtime.conflicting_terminal_observation",
            created_at=conflict_evidence.received_at,
            updated_at=conflict_evidence.received_at,
        )
        incident, created = uow.runtimes.add_integrity_incident_with_created(candidate_incident)
        if not _late_terminal_incident_matches(incident, candidate_incident):
            raise RuntimeExecutionConflict("Late-terminal incident is inconsistent")
        if created:
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.integrity-incident.opened",
                    tenant_id=self._tenant_id,
                    aggregate_id=incident.id,
                    producer="agentmesh-managed-runtime-worker-v1",
                    payload={
                        "incident_id": str(incident.id),
                        "tenant_id": self._tenant_id,
                        "runtime_execution_id": str(incident.runtime_execution_id),
                        "accepted_observation_id": incident.accepted_observation_id,
                        "accepted_observation_digest": incident.accepted_observation_digest,
                        "accepted_phase": incident.accepted_phase.value,
                        "conflicting_observation_id": incident.conflicting_observation_id,
                        "conflicting_observation_digest": incident.conflicting_observation_digest,
                        "conflicting_phase": incident.conflicting_phase.value,
                        "status": incident.status.value,
                        "reason": incident.reason,
                    },
                )
            )
            kind = LateTerminalObservationResultKind.INCIDENT_OPENED
        else:
            kind = LateTerminalObservationResultKind.INCIDENT_REPLAY
        return LateTerminalObservationResult(
            kind=kind,
            accepted_anchor=anchor,
            conflicting_observation=conflict_evidence,
            incident=incident,
        )

    def record_observation_in_uow(
        self,
        uow: UnitOfWork,
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
        """Record Runtime evidence in a caller-owned atomic transaction."""
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

    def mark_execution_dispatching(
        self,
        *,
        execution_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        """Persist the provider side-effect boundary before dispatch."""
        self._require_enabled()
        with self._uow_factory() as uow:
            execution = uow.runtimes.get_execution(
                execution_id, tenant_id=self._tenant_id, for_update=True
            )
            if execution is None:
                raise RuntimeExecutionNotFound("Runtime execution was not found")
            if (
                execution.current_owner_attempt_id != attempt_id
                or execution.current_fencing_token != fencing_token
            ):
                raise RuntimeExecutionConflict("Runtime execution owner is stale")
            updated = execution.apply_observation(
                phase=RuntimeExecutionPhase.DISPATCHING,
                provider_sequence=execution.provider_sequence,
                now=now or _now(),
            )
            uow.runtimes.save_execution(updated, tenant_id=self._tenant_id)
            uow.commit()
            return updated

    def request_lifecycle_operation(
        self,
        *,
        execution_id: UUID,
        operation_id: str,
        operation: RuntimeLifecycleOperation,
        deadline: datetime,
        now: datetime | None = None,
    ) -> RuntimeLifecycleStatus:
        self._require_enabled()
        timestamp = now or _now()
        if (
            type(timestamp) is not datetime
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise InvalidTaskInput("Runtime lifecycle timestamp is invalid")
        timestamp = timestamp.astimezone(timezone.utc)
        if (
            type(execution_id) is not UUID
            or type(operation_id) is not str
            or not operation_id.strip()
            or len(operation_id) > 512
            or type(operation) is not RuntimeLifecycleOperation
            or type(deadline) is not datetime
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            raise InvalidTaskInput("Runtime lifecycle operation identity is invalid")
        if operation is RuntimeLifecycleOperation.CANCEL and operation_id != (
            f"runtime-cancel:{execution_id}:v1"
        ):
            raise InvalidTaskInput("Runtime cancellation operation identity is invalid")
        deadline_utc = deadline.astimezone(timezone.utc)
        intent = {
            "tenant_id": self._tenant_id,
            "runtime_execution_id": str(execution_id),
            "operation_id": operation_id,
            "operation": operation.value,
            "deadline": deadline_utc.isoformat(),
        }
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
                if (
                    existing.operation is not operation
                    or existing.deadline.astimezone(timezone.utc) != deadline_utc
                    or existing.intent_digest != digest
                ):
                    raise RuntimeExecutionConflict("Lifecycle operation identity conflicts")
                return RuntimeLifecycleStatus(existing.status)
            if deadline <= timestamp:
                raise InvalidTaskInput("Runtime lifecycle deadline is invalid")
            lifecycle = RuntimeLifecycleIntent(
                id=uuid4(),
                tenant_id=self._tenant_id,
                runtime_execution_id=execution_id,
                operation_id=operation_id,
                operation=operation,
                intent_digest=digest,
                status=RuntimeLifecycleStatus.REQUESTED,
                deadline=deadline_utc,
                receipt_summary=None,
                version=1,
                created_at=timestamp,
                updated_at=timestamp,
                next_attempt_at=timestamp,
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
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.lifecycle.requested",
                    tenant_id=self._tenant_id,
                    aggregate_id=execution_id,
                    producer="agentmesh-runtime-lifecycle-command-v1",
                    payload={
                        "tenant_id": self._tenant_id,
                        "runtime_execution_id": str(execution_id),
                        "operation_id": operation_id,
                        "operation": operation.value,
                        "deadline": deadline_utc.isoformat(),
                    },
                )
            )
            uow.commit()
            return RuntimeLifecycleStatus.REQUESTED
