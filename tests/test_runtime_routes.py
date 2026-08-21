from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.api.runtime_routes import (
    get_execution,
    list_observations,
    list_runtimes,
    list_versions,
)
from agentmesh.api.security import get_principal_context
from agentmesh.application.runtime_reconciliation import (
    RuntimeOutcomeReconciliationResult,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import (
    FeatureDisabled,
    RuntimeExecutionNotFound,
    RuntimeRegistryConflict,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeRegistration,
    RuntimeRegistrationStatus,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
    RuntimeVisibility,
)
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest


def _principal(
    tenant_id: str, roles: frozenset[Role] = frozenset({Role.OPERATOR})
) -> PrincipalContext:
    return PrincipalContext(
        principal_id=str(uuid4()),
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=roles,
        authenticated=True,
        authentication_method="test",
    )


class _ProjectionService:
    tenant_id = "tenant-routes"

    def __init__(self) -> None:
        self.principal_ids: list[str | None] = []
        self.pages: list[tuple[str, int, int]] = []
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        owner = uuid4()
        self.registration = RuntimeRegistration(
            id=uuid4(),
            tenant_id=None,
            name="platform-runtime",
            owner_principal_id=owner,
            visibility=RuntimeVisibility.PLATFORM,
            status=RuntimeRegistrationStatus.ACTIVE,
            default_version_id=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.version = RuntimeVersion(
            id=uuid4(),
            runtime_id=self.registration.id,
            api_version=1,
            adapter_kind="python-in-process",
            artifact_digest="a" * 64,
            configuration_digest="b" * 64,
            descriptor={
                "schema_name": "agentmesh.runtime-descriptor",
                "schema_version": 1,
                "runtime_key": "test.runtime",
                "display_name": "Test Runtime",
                "adapter_kind": "python-in-process",
                "capabilities": {},
                "limits": {},
            },
            trust_profile=RuntimeTrustProfile.BUILT_IN,
            compatibility={},
            status=RuntimeVersionStatus.PUBLISHED,
            created_at=now,
            published_at=now,
        )
        self.execution = RuntimeExecution.prepare(
            tenant_id=self.tenant_id,
            run_id=uuid4(),
            runtime_version_id=self.version.id,
            assignment_id=uuid4(),
            assignment_digest="c" * 64,
            dispatch_key="runtime-dispatch:routes:fixed",
            dispatch_digest="d" * 64,
            now=now,
        ).apply_observation(
            phase=RuntimeExecutionPhase.DISPATCHING,
            provider_sequence=1,
            provider_execution_ref="opaque-provider",
            checkpoint_ref="opaque-checkpoint",
            workspace_ref="opaque-workspace",
            now=now,
        )

    def list_registrations(self, **kwargs):
        self.principal_ids.append(kwargs["principal_id"])
        self.pages.append(("runtimes", kwargs["limit"], kwargs["offset"]))
        return [self.registration]

    def list_versions(self, runtime_id, **kwargs):
        self.principal_ids.append(kwargs["principal_id"])
        return [self.version]

    def get_execution(self, execution_id):
        return self.execution

    def list_observations(self, execution_id, **kwargs):
        self.pages.append(("observations", kwargs["limit"], kwargs["offset"]))
        return [
            {
                "id": uuid4(),
                "observation_id": "event-1",
                "observation_digest": "e" * 64,
                "assignment_id": self.execution.assignment_id,
                "assignment_digest": self.execution.assignment_digest,
                "provider_sequence": 1,
                "phase": "DISPATCHING",
                "observed_at": self.execution.created_at,
                "received_at": self.execution.created_at,
                "safe_summary": None,
                "processing_outcome": "APPLIED",
                "provider_event_present": False,
            }
        ]


class _ErrorService(_ProjectionService):
    def list_registrations(self, **kwargs):
        raise RuntimeRegistryConflict("conflict")

    def get_execution(self, execution_id):
        raise RuntimeExecutionNotFound("missing")


class _ReconciliationService:
    tenant_id = "test-tenant"

    def __init__(self, execution: RuntimeExecution) -> None:
        self.execution = execution
        self.calls = []

    def reconcile_outcome(self, execution_id, **kwargs):
        self.calls.append((execution_id, kwargs))
        resolution = TaskResolution.create(
            task_id=self.execution.run_id,
            action=TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
            actor=kwargs["principal"].principal_id,
            reason=kwargs["reason"],
            previous_status=TaskStatus.RECONCILIATION_REQUIRED,
            resulting_status=TaskStatus.COMPLETED,
            previous_error="runtime.unknown",
        )
        return RuntimeOutcomeReconciliationResult(self.execution, resolution)


def test_runtime_routes_use_authenticated_principal_and_redact_opaque_refs() -> None:
    service = _ProjectionService()
    principal = _principal(service.tenant_id)
    runtimes = list_runtimes(service, principal, limit=1, offset=0)
    versions = list_versions(service.registration.id, service, principal)
    execution = get_execution(service.execution.id, service, principal)
    observations = list_observations(service.execution.id, service, principal, limit=1, offset=0)

    assert service.principal_ids == [UUID(principal.principal_id), UUID(principal.principal_id)]
    assert runtimes[0].name == "platform-runtime"
    assert versions[0].id == service.version.id
    assert execution.provider_execution_ref_present is True
    assert execution.checkpoint_ref_present is True
    assert execution.workspace_ref_present is True
    assert "opaque-provider" not in execution.model_dump_json()
    assert len(observations) == 1


def test_runtime_service_is_gate_off_by_default() -> None:
    service = RuntimeRegistryService(
        uow_factory=lambda: pytest.fail("gate must stop before opening a UoW"),
        tenant_id="tenant-routes",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with pytest.raises(FeatureDisabled):
        service.list_registrations()


def test_runtime_http_routes_apply_gate_principal_and_paging(application_container) -> None:
    service = _ProjectionService()
    service.tenant_id = "test-tenant"
    application_container.runtime_service = service
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true"
    )
    principal = _principal(service.tenant_id)
    application = create_app(application_container)
    application.dependency_overrides[get_principal_context] = lambda: principal
    with TestClient(application) as client:
        runtimes = client.get("/api/v1/runtimes?limit=1&offset=2")
        versions = client.get(f"/api/v1/runtimes/{service.registration.id}/versions")
        execution = client.get(f"/api/v1/runtime-executions/{service.execution.id}")
        observations = client.get(
            f"/api/v1/runtime-executions/{service.execution.id}/observations?limit=1&offset=2"
        )
        application.dependency_overrides[get_principal_context] = lambda: _principal(
            "another-tenant"
        )
        cross_tenant = client.get("/api/v1/runtimes")
    assert runtimes.status_code == 200
    assert versions.status_code == 200
    assert execution.status_code == 200
    assert observations.status_code == 200
    assert cross_tenant.status_code == 403
    assert service.principal_ids[-2:] == [UUID(principal.principal_id)] * 2
    assert ("runtimes", 1, 2) in service.pages
    assert ("observations", 1, 2) in service.pages


def test_runtime_http_maps_not_found_and_conflict(application_container) -> None:
    service = _ErrorService()
    service.tenant_id = "test-tenant"
    application_container.runtime_service = service
    principal = _principal(service.tenant_id)
    application = create_app(application_container)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true"
    )
    application.dependency_overrides[get_principal_context] = lambda: principal
    with TestClient(application) as client:
        assert client.get("/api/v1/runtimes").status_code == 409
        assert client.get(f"/api/v1/runtime-executions/{uuid4()}").status_code == 404


def test_runtime_http_feature_and_rbac_dependencies_are_not_bypassed(application_container) -> None:
    service = _ProjectionService()
    service.tenant_id = "test-tenant"
    application_container.runtime_service = service
    principal = _principal(service.tenant_id)
    application_container.feature_gates = FeatureGateSet.from_config("minimal")
    feature_off = create_app(application_container)
    feature_off.dependency_overrides[get_principal_context] = lambda: principal
    with TestClient(feature_off) as client:
        assert client.get("/api/v1/runtimes").status_code == 403

    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "managed_agent_runtime=true"
    )
    no_permission = create_app(application_container)
    no_permission.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id, frozenset({Role.AGENT_AUTHOR})
    )
    with TestClient(no_permission) as client:
        assert client.get("/api/v1/runtimes").status_code == 403


def test_runtime_reconciliation_http_requires_gate_permission_and_idempotency(
    application_container,
) -> None:
    projection = _ProjectionService()
    execution = projection.execution.apply_observation(
        phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        provider_sequence=2,
    )
    service = _ReconciliationService(execution)
    application_container.runtime_service = projection
    application_container.runtime_reconciliation_service = service
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
    )
    principal = _principal(service.tenant_id)
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="operator-evidence-1",
        output={"answer": 42},
    )
    payload = {
        "observation": jsonable_encoder(observation.to_dict()),
        "evidence_digest": canonical_digest(observation.to_dict()),
        "evidence_reference": "case://runtime/42",
        "reason": "Provider support confirmed completion",
    }
    application = create_app(application_container)
    application.dependency_overrides[get_principal_context] = lambda: principal
    with TestClient(application) as client:
        missing_key = client.post(
            f"/api/v1/runtime-executions/{execution.id}/reconcile-outcome", json=payload
        )
        accepted = client.post(
            f"/api/v1/runtime-executions/{execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "runtime-reconcile-1"},
        )
    assert missing_key.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["resolution"]["action"] == "RECONCILE_RUNTIME_SUCCEEDED"
    assert service.calls[0][1]["idempotency_key"] == "runtime-reconcile-1"


def test_runtime_reconciliation_http_rejects_cross_tenant_and_missing_permission(
    application_container,
) -> None:
    projection = _ProjectionService()
    service = _ReconciliationService(projection.execution)
    application_container.runtime_service = projection
    application_container.runtime_reconciliation_service = service
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
    )
    application = create_app(application_container)
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(projection.execution.id),
        assignment_id=str(projection.execution.assignment_id),
        assignment_digest=projection.execution.assignment_digest,
        phase=RuntimePhase.FAILED,
        observed_at=datetime.now(timezone.utc),
        snapshot_digest="a" * 64,
    )
    payload = {
        "observation": jsonable_encoder(observation.to_dict()),
        "evidence_digest": canonical_digest(observation.to_dict()),
        "evidence_reference": "case://runtime/failure",
        "reason": "Confirmed failure",
    }
    application.dependency_overrides[get_principal_context] = lambda: _principal(
        "another-tenant"
    )
    with TestClient(application) as client:
        cross_tenant = client.post(
            f"/api/v1/runtime-executions/{projection.execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "cross-tenant"},
        )
    application.dependency_overrides[get_principal_context] = lambda: _principal(
        service.tenant_id, frozenset({Role.AGENT_AUTHOR})
    )
    with TestClient(application) as client:
        denied = client.post(
            f"/api/v1/runtime-executions/{projection.execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "denied"},
        )
    assert cross_tenant.status_code == 403
    assert denied.status_code == 403
    assert service.calls == []
