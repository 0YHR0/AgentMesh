from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.api.app import create_app
from agentmesh.application.observability_services import UsageQueryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.bootstrap import build_worker_container
from agentmesh.config import Settings
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidFeatureConfiguration,
    InvalidTaskInput,
)
from agentmesh.domain.observability import TaskUsage, UsageRecord, UsageSource
from agentmesh.domain.tasks import AttemptStatus, Task, TaskAttempt, TaskRun, utc_now
from agentmesh.features import FeatureGateSet
from agentmesh.observability import LangfuseAttemptTelemetry
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import InMemoryUnitOfWorkFactory


class _UsageReportingExecutor:
    def __init__(self, task_service: TaskApplicationService | None = None) -> None:
        self._task_service = task_service
        self.calls = 0

    def execute(self, *, objective, input, context):
        self.calls += 1
        context.report_usage(
            provider="openai",
            model="gpt-test",
            usage_details={"input": 120, "output": 30, "total": 150},
            cost_details_micros={"input": 240, "output": 180, "total": 420},
            currency="usd",
            source=UsageSource.PROVIDER,
            pricing_version="2026-07",
        )
        if self._task_service is not None:
            self._task_service.pause_task(context.task_id)
        return {"objective": objective, "reported": True}


def _execution_service(
    uow_factory: InMemoryUnitOfWorkFactory,
    executor: _UsageReportingExecutor,
) -> RunExecutionService:
    return RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=executor,
            checkpointer=InMemorySaver(),
        ),
        worker_id="usage-worker",
        consumer_name="usage-worker-v1",
        lease_duration=timedelta(minutes=5),
    )


def test_attempt_trace_and_usage_are_persisted_and_queryable(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
    application_container,
) -> None:
    task_id = task_service.create_task("Measure a model call").task.id
    task_service.request_run(task_id)
    executor = _UsageReportingExecutor()

    assert _execution_service(uow_factory, executor).process(uow_factory.store.outbox[0]) is True

    aggregate = task_service.get_task(task_id)
    attempt = aggregate.attempts[0]
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.trace_id == attempt.id.hex
    assert len(attempt.trace_id) == 32

    usage = UsageQueryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    ).get_task_usage(task_id)
    assert usage.usage_details == {"input": 120, "output": 30, "total": 150}
    assert usage.cost_details_micros_by_currency == {
        "USD": {"input": 240, "output": 180, "total": 420}
    }
    assert usage.records[0].attempt_id == attempt.id
    assert usage.records[0].trace_id == attempt.trace_id

    with TestClient(create_app(application_container)) as client:
        response = client.get(f"/api/v1/tasks/{task_id}/usage")

    assert response.status_code == 200
    assert response.json()["records"][0]["pricing_version"] == "2026-07"
    assert response.json()["cost_details_micros_by_currency"]["USD"]["total"] == 420

    minimal_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(minimal_container)) as client:
        disabled = client.get(f"/api/v1/tasks/{task_id}/usage")
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "feature_disabled"
    assert "observability" in disabled.json()["message"]


def test_checkpoint_resume_does_not_duplicate_usage(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    executor = _UsageReportingExecutor(task_service)
    execution_service = _execution_service(uow_factory, executor)
    task_id = task_service.create_task("Pause after an observed model call").task.id
    task_service.request_run(task_id)
    original_wakeup = uow_factory.store.outbox[0]

    assert execution_service.process(original_wakeup) is True
    assert len(uow_factory.store.usage_records) == 1

    task_service.resume_task(task_id)
    resume_wakeup = next(
        item
        for item in reversed(uow_factory.store.outbox)
        if item.schema_name == original_wakeup.schema_name
        and item.message_id != original_wakeup.message_id
    )
    assert execution_service.process(resume_wakeup) is True

    aggregate = task_service.get_task(task_id)
    assert [attempt.status for attempt in aggregate.attempts] == [
        AttemptStatus.PAUSED,
        AttemptStatus.SUCCEEDED,
    ]
    assert executor.calls == 1
    assert len(uow_factory.store.usage_records) == 1
    record = next(iter(uow_factory.store.usage_records.values()))
    assert record.attempt_id == aggregate.attempts[0].id


@pytest.mark.parametrize(
    "usage_details",
    [
        {},
        {"input": -1},
        {"input": True},
        {"": 1},
    ],
)
def test_usage_record_rejects_ambiguous_or_invalid_buckets(usage_details) -> None:
    attempt = _attempt_fixture()
    with pytest.raises(InvalidTaskInput):
        UsageRecord.create(
            tenant_id="tenant",
            task_id=attempt[0].id,
            run_id=attempt[1].id,
            attempt_id=attempt[2].id,
            trace_id=attempt[2].trace_id,
            provider="provider",
            model="model",
            usage_details=usage_details,
        )


def test_usage_checkpoint_round_trip_and_multi_currency_summary() -> None:
    task, run, attempt = _attempt_fixture()
    usd = UsageRecord.create(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        trace_id=attempt.trace_id,
        provider="provider",
        model="model",
        usage_details={"total": 10},
        cost_details_micros={"total": 20},
    )
    eur = UsageRecord.create(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        trace_id=attempt.trace_id,
        provider="provider",
        model="model",
        usage_details={"total": 5},
        cost_details_micros={"total": 7},
        currency="EUR",
    )

    assert UsageRecord.from_checkpoint(usd.to_checkpoint()) == usd
    summary = TaskUsage.summarize(task.id, [usd, eur])
    assert summary.usage_details == {"total": 15}
    assert summary.cost_details_micros_by_currency == {
        "USD": {"total": 20},
        "EUR": {"total": 7},
    }


def test_usage_idempotency_key_cannot_hide_different_content() -> None:
    task, run, attempt = _attempt_fixture()
    record = UsageRecord.create(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        trace_id=attempt.trace_id,
        provider="provider",
        model="model",
        usage_details={"total": 1},
    )
    uow_factory = InMemoryUnitOfWorkFactory()
    with uow_factory() as uow:
        assert uow.usage_records.add_if_absent(record) is True
        uow.commit()

    with uow_factory() as uow:
        assert uow.usage_records.add_if_absent(record) is False
        with pytest.raises(IdempotencyConflict):
            uow.usage_records.add_if_absent(replace(record, model="other-model"))


class _Observation:
    def __init__(self) -> None:
        self.updates = []

    def update(self, **values) -> None:
        self.updates.append(values)


class _Context:
    def __init__(self, value=None) -> None:
        self.value = value
        self.exits = []

    def __enter__(self):
        return self.value

    def __exit__(self, *error_info):
        self.exits.append(error_info)
        return False


class _LangfuseClient:
    def __init__(self, *, fail=False) -> None:
        self.fail = fail
        self.calls = []
        self.shutdown_calls = 0

    def start_as_current_observation(self, **values):
        if self.fail:
            raise OSError("telemetry unavailable")
        observation = _Observation()
        self.calls.append((values, observation))
        return _Context(observation)

    def shutdown(self):
        self.shutdown_calls += 1
        if self.fail:
            raise OSError("telemetry unavailable")


def test_langfuse_adapter_exports_safe_correlation_and_usage_only() -> None:
    task, run, attempt = _attempt_fixture()
    client = _LangfuseClient()
    propagated = []

    def propagate(**values):
        propagated.append(values)
        return _Context()

    telemetry = LangfuseAttemptTelemetry(client, propagate)
    with telemetry.observe_attempt(task, run, attempt):
        telemetry.record_usage(
            UsageRecord.create(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
                attempt_id=attempt.id,
                trace_id=attempt.trace_id,
                provider="openai",
                model="gpt-test",
                usage_details={"input": 10, "output": 2},
                cost_details_micros={"total": 25},
            )
        )
    telemetry.close()

    root, generation = [values for values, _observation in client.calls]
    assert root["trace_context"] == {"trace_id": attempt.trace_id}
    assert root["as_type"] == "agent"
    assert "input" not in root and "output" not in root
    assert task.objective not in repr(root)
    assert propagated[0]["session_id"] == str(task.id)
    assert propagated[0]["metadata"]["tenant_key"] != task.tenant_id
    assert task.tenant_id not in propagated[0]["metadata"].values()
    assert generation["as_type"] == "generation"
    assert generation["usage_details"] == {"input": 10, "output": 2}
    assert generation["cost_details"] == {"total": 0.000025}
    assert "input" not in generation or generation["input"] is None
    assert client.shutdown_calls == 1


def test_langfuse_outage_never_breaks_business_execution() -> None:
    task, run, attempt = _attempt_fixture()
    telemetry = LangfuseAttemptTelemetry(_LangfuseClient(fail=True), lambda **_: _Context())
    executed = False

    with telemetry.observe_attempt(task, run, attempt):
        executed = True
    telemetry.record_usage(
        UsageRecord.create(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            trace_id=attempt.trace_id,
            provider="provider",
            model="model",
            usage_details={"total": 1},
        )
    )
    telemetry.close()

    assert executed is True


def test_langfuse_adapter_preserves_business_exception() -> None:
    task, run, attempt = _attempt_fixture()
    client = _LangfuseClient()
    telemetry = LangfuseAttemptTelemetry(client, lambda **_: _Context())

    with pytest.raises(ValueError, match="business failure"):
        with telemetry.observe_attempt(task, run, attempt):
            raise ValueError("business failure")

    root_observation = client.calls[0][1]
    assert root_observation.updates == [
        {
            "level": "ERROR",
            "status_message": "AgentMesh workflow failed: ValueError",
        }
    ]


def test_langfuse_configuration_fails_before_runtime_resources_are_opened() -> None:
    missing_gate = Settings(
        _env_file=None,
        feature_profile="minimal",
        langfuse_enabled=True,
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    with pytest.raises(InvalidFeatureConfiguration, match="observability"):
        build_worker_container(missing_gate, worker_id="never-started")

    missing_credentials = Settings(
        _env_file=None,
        feature_profile="full",
        langfuse_enabled=True,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    with pytest.raises(InvalidFeatureConfiguration, match="PUBLIC_KEY"):
        build_worker_container(missing_credentials, worker_id="never-started")


def test_settings_accept_official_langfuse_environment_names(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-official")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-official")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example")

    settings = Settings(_env_file=None)

    assert settings.langfuse_public_key == "pk-official"
    assert settings.langfuse_secret_key == "sk-official"
    assert settings.langfuse_base_url == "https://langfuse.example"


def _attempt_fixture() -> tuple[Task, TaskRun, TaskAttempt]:
    task = Task.create(tenant_id="tenant", objective="private objective")
    run = TaskRun.request(task.id, "test-agent")
    task.queue(run.id)
    task.start(run.id)
    run.start()
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker",
        fencing_token=1,
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    return task, run, attempt
