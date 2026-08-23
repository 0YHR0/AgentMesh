from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_reconciliation import RuntimeOutcomeReconciliationService
from agentmesh.domain.errors import AuthorizationDenied, InvalidTaskInput
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

TENANT_ID = "runtime-reconciliation-unit"


def _principal(*, tenant_id: str = TENANT_ID, authenticated: bool = True) -> PrincipalContext:
    return PrincipalContext(
        principal_id="operator-unit",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=authenticated,
        authentication_method="test",
    )


def _service() -> RuntimeOutcomeReconciliationService:
    def reject_uow():
        raise AssertionError("invalid requests must be rejected before opening a UoW")

    return RuntimeOutcomeReconciliationService(
        uow_factory=reject_uow,
        tenant_id=TENANT_ID,
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
    )


def _observation(
    execution_id,
    *,
    phase: RuntimePhase = RuntimePhase.SUCCEEDED,
    output=None,
    usage=None,
) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(uuid4()),
        assignment_digest="a" * 64,
        phase=phase,
        observed_at=datetime.now(timezone.utc),
        snapshot_digest="b" * 64,
        output={"answer": 42} if output is None and phase is RuntimePhase.SUCCEEDED else output,
        usage={} if usage is None else usage,
    )


def _reconcile(service, execution_id, observation, **overrides):
    values = {
        "principal": _principal(),
        "observation": observation,
        "evidence_digest": canonical_digest(observation.to_dict()),
        "evidence_reference": "case://unit/evidence",
        "reason": "Provider support supplied canonical evidence",
        "idempotency_key": "unit-reconcile-1",
    }
    values.update(overrides)
    return service.reconcile_outcome(execution_id, **values)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (_principal(authenticated=False), AuthorizationDenied),
        (_principal(tenant_id="another-tenant"), AuthorizationDenied),
    ],
)
def test_runtime_reconciliation_rejects_invalid_principal_before_uow(principal, expected) -> None:
    execution_id = uuid4()
    observation = _observation(execution_id)
    with pytest.raises(expected):
        _reconcile(_service(), execution_id, observation, principal=principal)


def test_runtime_reconciliation_rejects_nonterminal_and_wrong_execution_before_uow() -> None:
    service = _service()
    execution_id = uuid4()
    running = _observation(execution_id, phase=RuntimePhase.RUNNING)
    with pytest.raises(InvalidTaskInput, match="known terminal"):
        _reconcile(service, execution_id, running)

    other_execution = _observation(uuid4())
    with pytest.raises(InvalidTaskInput, match="execution identity"):
        _reconcile(service, execution_id, other_execution)


def test_runtime_reconciliation_rejects_digest_and_success_shape_before_uow() -> None:
    service = _service()
    execution_id = uuid4()
    observation = _observation(execution_id)
    with pytest.raises(InvalidTaskInput, match="canonical observation digest"):
        _reconcile(service, execution_id, observation, evidence_digest="f" * 64)

    non_mapping = _observation(execution_id, output=["not", "a", "mapping"])
    with pytest.raises(InvalidTaskInput, match="mapping output"):
        _reconcile(service, execution_id, non_mapping)

    billed = _observation(execution_id, usage={"input_tokens": 1})
    with pytest.raises(InvalidTaskInput, match="empty usage"):
        _reconcile(service, execution_id, billed)
