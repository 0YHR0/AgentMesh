from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.api.runtime_routes import reconcile_coordinated_runtime_outcome
from agentmesh.api.security import get_principal_context
from agentmesh.application.coordinated_runtime_reconciliation import (
    CoordinatedRuntimeReconciliationResult,
)
from agentmesh.bootstrap import build_api_container
from agentmesh.domain.errors import IdempotencyConflict, RuntimeExecutionConflict
from agentmesh.domain.identity import PrincipalContext, Role
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest
from tests.test_runtime_routes import _principal, _ProjectionService


class _CoordinatedReconciliationService:
    def __init__(self, execution, *, error: Exception | None = None) -> None:
        self.execution = execution
        self.error = error
        self.calls: list[dict[str, object]] = []

    def reconcile_known_terminal(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        resolution = TaskResolution.create(
            task_id=kwargs["task_id"],
            action=TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
            actor=kwargs["principal"].principal_id,
            reason=kwargs["reason"],
            previous_status=TaskStatus.RECONCILIATION_REQUIRED,
            resulting_status=TaskStatus.COMPLETED,
            previous_error="runtime.unknown",
        )
        return CoordinatedRuntimeReconciliationResult(self.execution, resolution)


def _command(execution) -> tuple[dict[str, object], RuntimeObservation]:
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="operator-coordinated-evidence-1",
        output={"answer": 42},
    )
    return (
        {
            "run_id": str(execution.run_id),
            "attempt_id": str(uuid4()),
            "fencing_token": 7,
            "observation": jsonable_encoder(observation.to_dict()),
            "evidence_digest": canonical_digest(observation.to_dict()),
            "evidence_reference": "case://runtime/coordinated/42",
            "reason": "Provider support confirmed coordinated completion",
        },
        observation,
    )


def _application(application_container, service, principal: PrincipalContext):
    application_container.coordinated_runtime_reconciliation_service = service
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
    )
    application = create_app(application_container)
    application.dependency_overrides[get_principal_context] = lambda: principal
    return application


def test_task_scoped_reconciliation_calls_only_coordinated_service_with_explicit_identity(
    application_container,
) -> None:
    projection = _ProjectionService()
    execution = projection.execution.apply_observation(
        phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        provider_sequence=2,
    )
    service = _CoordinatedReconciliationService(execution)
    task_id = uuid4()
    payload, observation = _command(execution)
    principal = _principal("test-tenant")
    application = _application(application_container, service, principal)

    with TestClient(application) as client:
        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/runtime-executions/{execution.id}/reconcile-outcome",
            json=payload,
        )
        accepted = client.post(
            f"/api/v1/tasks/{task_id}/runtime-executions/{execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "coordinated-runtime-reconcile-1"},
        )

    assert missing_key.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["resolution"]["action"] == "RECONCILE_RUNTIME_SUCCEEDED"
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["tenant_id"] == principal.tenant_id
    assert call["task_id"] == task_id
    assert call["run_id"] == execution.run_id
    assert call["runtime_execution_id"] == execution.id
    assert call["fencing_token"] == 7
    assert call["observation"] == observation
    assert call["idempotency_key"] == "coordinated-runtime-reconcile-1"
    assert isinstance(call["received_at"], datetime)


def test_task_scoped_reconciliation_http_enforces_features_and_permission(
    application_container,
) -> None:
    projection = _ProjectionService()
    service = _CoordinatedReconciliationService(projection.execution)
    payload, _ = _command(projection.execution)
    path = f"/api/v1/tasks/{uuid4()}/runtime-executions/{projection.execution.id}/reconcile-outcome"
    application_container.coordinated_runtime_reconciliation_service = service
    application_container.feature_gates = FeatureGateSet.from_config("minimal")
    feature_off = create_app(application_container)
    feature_off.dependency_overrides[get_principal_context] = lambda: _principal("test-tenant")
    with TestClient(feature_off) as client:
        disabled = client.post(path, json=payload, headers={"Idempotency-Key": "disabled"})

    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
    )
    denied_app = create_app(application_container)
    denied_app.dependency_overrides[get_principal_context] = lambda: _principal(
        "test-tenant", frozenset({Role.AGENT_AUTHOR})
    )
    with TestClient(denied_app) as client:
        denied = client.post(path, json=payload, headers={"Idempotency-Key": "denied"})

    assert disabled.status_code == 403
    assert denied.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RuntimeExecutionConflict("sensitive projection detail"),
            {
                "code": "runtime_conflict",
                "message": "Coordinated Runtime reconciliation is unavailable",
            },
        ),
        (
            IdempotencyConflict("sensitive replay detail"),
            {
                "code": "idempotency_conflict",
                "message": "Idempotency-Key request conflicts",
            },
        ),
    ],
)
def test_task_scoped_reconciliation_maps_conflicts_to_bounded_409(
    application_container, error, expected
) -> None:
    projection = _ProjectionService()
    service = _CoordinatedReconciliationService(projection.execution, error=error)
    payload, _ = _command(projection.execution)
    application = _application(application_container, service, _principal("test-tenant"))
    with TestClient(application) as client:
        response = client.post(
            f"/api/v1/tasks/{uuid4()}/runtime-executions/{projection.execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "safe-conflict"},
        )
    assert response.status_code == 409
    assert response.json() == expected


def test_task_scoped_reconciliation_maps_untrusted_observation_to_bounded_422(
    application_container,
) -> None:
    projection = _ProjectionService()
    service = _CoordinatedReconciliationService(projection.execution)
    payload, _ = _command(projection.execution)
    payload["observation"] = {"untrusted_field": "do-not-reflect-me"}
    application = _application(application_container, service, _principal("test-tenant"))
    with TestClient(application) as client:
        response = client.post(
            f"/api/v1/tasks/{uuid4()}/runtime-executions/{projection.execution.id}/reconcile-outcome",
            json=payload,
            headers={"Idempotency-Key": "safe-invalid"},
        )
    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_task_input",
        "message": "Coordinated Runtime reconciliation observation is invalid",
    }
    assert service.calls == []


def test_task_scoped_route_has_one_direct_service_caller_and_no_projection_preread() -> None:
    tree = ast.parse(inspect.getsource(reconcile_coordinated_runtime_outcome))
    attribute_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert attribute_calls.count("reconcile_known_terminal") == 1
    assert not {
        "get_execution",
        "get_task",
        "get_run",
        "list_executions_for_run",
    }.intersection(attribute_calls)


def test_api_bootstrap_wires_coordinated_reconciliation_without_opening_gate() -> None:
    source = inspect.getsource(build_api_container)
    tree = ast.parse(source)
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CoordinatedRuntimeReconciliationService"
    ]
    assert len(constructors) == 1
    returned_fields = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ApplicationContainer"
        for keyword in node.keywords
    }
    assert "coordinated_runtime_reconciliation_service" in returned_fields
    assert "MANAGED_RUNTIME_COORDINATED_CUTOVER" not in source
