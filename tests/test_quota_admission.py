from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.quota_services import (
    QuotaAdmissionRejected,
    QuotaController,
    QuotaPolicyService,
)
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.tasks import Task, TaskAttempt, utc_now
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _attempt() -> TaskAttempt:
    return TaskAttempt.lease(
        run_id=Task.create(tenant_id="unused", objective="unused").id,
        worker_id="quota-test-worker",
        fencing_token=1,
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )


def test_hierarchical_quota_reserves_and_releases_both_scopes() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    policies = QuotaPolicyService(uow_factory, "acme")
    tenant = policies.put_policy(
        scope=QuotaScope.TENANT,
        project_id=None,
        max_concurrent_attempts=2,
        weight=3,
        created_by="admin",
    )
    project = policies.put_policy(
        scope=QuotaScope.PROJECT,
        project_id="research",
        max_concurrent_attempts=1,
        weight=7,
        created_by="admin",
    )
    task = Task.create(tenant_id="acme", project_id="research", objective="Investigate")
    first = _attempt()
    second = _attempt()

    with uow_factory() as uow:
        QuotaController.reserve_attempt(uow, task, first)
        uow.commit()

    status = {value.policy.scope: value for value in policies.list_status()}
    assert status[QuotaScope.TENANT].active_reservations == 1
    assert status[QuotaScope.PROJECT].active_reservations == 1
    assert tenant.policy.weight == 3
    assert project.policy.weight == 7

    with uow_factory() as uow, pytest.raises(QuotaAdmissionRejected) as rejected:
        QuotaController.reserve_attempt(uow, task, second)
    assert rejected.value.policy.scope is QuotaScope.PROJECT

    with uow_factory() as uow:
        QuotaController.release_attempt(uow, first)
        uow.commit()
    assert all(value.active_reservations == 0 for value in policies.list_status())


def test_policy_replacement_keeps_old_version_capacity_in_scope() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    service = QuotaPolicyService(uow_factory, "acme")
    first_policy = service.put_policy(
        scope=QuotaScope.PROJECT,
        project_id="platform",
        max_concurrent_attempts=2,
        weight=1,
        created_by="admin",
    )
    task = Task.create(tenant_id="acme", project_id="platform", objective="Build")
    attempt = _attempt()
    with uow_factory() as uow:
        QuotaController.reserve_attempt(uow, task, attempt)
        uow.commit()

    replacement = service.put_policy(
        scope=QuotaScope.PROJECT,
        project_id="platform",
        max_concurrent_attempts=1,
        weight=4,
        created_by="admin-2",
    )
    assert replacement.policy.version == first_policy.policy.version + 1
    assert replacement.active_reservations == 1
    with uow_factory() as uow, pytest.raises(QuotaAdmissionRejected):
        QuotaController.reserve_attempt(uow, task, _attempt())


def test_quota_api_is_feature_gated_and_versions_policies(application_container) -> None:
    with TestClient(create_app(application_container)) as client:
        disabled = client.get("/api/v1/quotas/policies")
        assert disabled.status_code == 403
        assert disabled.json()["code"] == "feature_disabled"

        application_container.feature_gates = FeatureGateSet.from_config(
            "minimal", "identity_rbac=true,quota_admission=true"
        )
        created = client.put(
            "/api/v1/quotas/policies",
            json={
                "scope": "PROJECT",
                "project_id": "api",
                "max_concurrent_attempts": 4,
                "weight": 2,
            },
        )
        assert created.status_code == 200
        assert created.json()["version"] == 1
        assert created.json()["project_id"] == "api"
        assert client.get("/api/v1/quotas/policies").json()[0]["weight"] == 2
