from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.quota_services import (
    QuotaAdmissionRejected,
    QuotaController,
    QuotaPolicyService,
)
from agentmesh.config import get_settings
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun, utc_now
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_project_quota_serializes_concurrent_attempt_admission() -> None:
    suffix = uuid4().hex
    settings = get_settings().model_copy(update={"tenant_id": f"quota-{suffix}"})
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    try:
        QuotaPolicyService(factory, settings.tenant_id).put_policy(
            scope=QuotaScope.PROJECT,
            project_id="shared",
            max_concurrent_attempts=1,
            weight=1,
            created_by="postgres-test",
        )
        work: list[tuple[Task, TaskRun]] = []
        with factory() as uow:
            for index in range(2):
                task = Task.create(
                    tenant_id=settings.tenant_id,
                    project_id="shared",
                    objective=f"Concurrent {index}",
                )
                run = TaskRun.request(task.id, "quota-agent")
                uow.tasks.add(task)
                uow.flush()
                uow.runs.add(run)
                work.append((task, run))
            uow.commit()

        def admit(index: int) -> bool:
            task_snapshot, run = work[index]
            with factory() as uow:
                task = uow.tasks.get(task_snapshot.id, for_update=True)
                assert task is not None
                attempt = TaskAttempt.lease(
                    run_id=run.id,
                    worker_id=f"worker-{index}",
                    fencing_token=1,
                    lease_expires_at=utc_now() + timedelta(minutes=5),
                )
                uow.attempts.add(attempt)
                try:
                    QuotaController.reserve_attempt(uow, task, attempt)
                except QuotaAdmissionRejected:
                    return False
                uow.commit()
                return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = list(pool.map(admit, range(2)))
        assert sorted(admitted) == [False, True]
    finally:
        engine.dispose()
