import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.authority_cohorts import AuthorityCohortResolver
from agentmesh.application.coordinated_runtime_delivery import DeliveryInProgress
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.bootstrap import (
    WorkerContainer,
    _build_coordinated_runtime_delivery_graph,
)
from agentmesh.domain.errors import InvalidFeatureConfiguration, InvalidMessage
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.features import FeatureGateSet
from agentmesh.workers.execution import RedisRunWorker


def test_worker_rejects_malformed_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("not-json")


def test_worker_rejects_non_object_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("[]")


def test_worker_keeps_delivery_in_progress_message_pending() -> None:
    task_id = uuid4()
    run_id = uuid4()
    envelope = MessageEnvelope.run_requested(
        tenant_id="tenant-a", task_id=task_id, run_id=run_id
    )

    class _Redis:
        def __init__(self):
            self.acked = []
            self.dead_letters = []

        def xgroup_create(self, *args, **kwargs):
            return None

        def xautoclaim(self, *args, **kwargs):
            return ("0-0", [], [])

        def xreadgroup(self, *args, **kwargs):
            return [("runs", [("1-0", {"envelope": json.dumps(envelope.to_dict())})])]

        def xack(self, *args):
            self.acked.append(args)
            return 1

        def xadd(self, *args):
            self.dead_letters.append(args)
            return "2-0"

    class _Execution:
        def process(self, value):
            assert value == envelope
            raise DeliveryInProgress(task_id, run_id)

    redis = _Redis()
    worker = RedisRunWorker(
        redis_client=redis,
        execution_service=_Execution(),
        stream_name="runs",
        group_name="workers",
        consumer_id="worker-1",
        dead_letter_stream="dead",
        block_ms=0,
        pending_idle_ms=1_000,
    )

    assert worker.run_once() == 0
    assert redis.acked == []
    assert redis.dead_letters == []


def test_coordinated_delivery_graph_is_gate_off_and_reuses_worker_collaborators() -> None:
    gate_off = FeatureGateSet.from_config("full")
    assert (
        _build_coordinated_runtime_delivery_graph(
            uow_factory=lambda: None,
            worker_id="worker-1",
            consumer_name="consumer-1",
            lease_duration=timedelta(minutes=5),
            cancel_deadline_window=timedelta(minutes=5),
            feature_gates=gate_off,
            runtime_adapter=None,
            worker_runtime_registry=None,
            managed_execution_service=None,
            worker_authority_cohort_resolver=None,
            worker_coordinated_scheduler=None,
            worker_convergence_service=None,
            worker_unknown_service=None,
            runtime_memory_service=None,
            aggregate_locker=None,
        )
        is None
    )

    gates = FeatureGateSet.from_config(
        "full",
        "managed_runtime_worker=true,managed_runtime_coordinated_cutover=true",
    )
    resolver = AuthorityCohortResolver(feature_gates=gates)
    scheduler = CoordinatedScheduler(
        supervisor_agent_id="supervisor",
        authority_cohort_resolver=resolver,
    )
    memory = object()

    class _Adapter:
        def validate(self, assignment):
            return None

        def dispatch(self, assignment, *, dispatch_key):
            return None

        def inspect(self, handle):
            return None

    class _Registry:
        def get_handle_snapshot(self, execution_id):
            return None

    class _Managed:
        def assignment_for_delivery(self, lease, work_item):
            return None

        def bind_delivery_context(self, assignment, lease, work_item):
            return None

    convergence = SimpleNamespace(
        _runtime_memory_service=memory,
        apply_delivery_terminal=lambda **kwargs: None,
    )
    unknown = SimpleNamespace(park_delivery_unknown=lambda **kwargs: None)
    locker = SimpleNamespace(lock=lambda *args, **kwargs: None)
    graph = _build_coordinated_runtime_delivery_graph(
        uow_factory=lambda: None,
        worker_id="worker-1",
        consumer_name="consumer-1",
        lease_duration=timedelta(minutes=5),
        cancel_deadline_window=timedelta(minutes=5),
        feature_gates=gates,
        runtime_adapter=_Adapter(),
        worker_runtime_registry=_Registry(),
        managed_execution_service=_Managed(),
        worker_authority_cohort_resolver=resolver,
        worker_coordinated_scheduler=scheduler,
        worker_convergence_service=convergence,
        worker_unknown_service=unknown,
        runtime_memory_service=memory,
        aggregate_locker=locker,
    )

    assert graph is not None
    assert graph.delivery_service._acquisition is graph.acquisition_service
    assert graph.delivery_service._dispatch is graph.dispatch_service
    assert graph.delivery_service._predispatch_failure_service is (
        graph.predispatch_failure_service
    )
    assert graph.delivery_service._convergence is convergence
    assert graph.delivery_service._unknown is unknown
    assert graph.acquisition_service._aggregate_locker is locker
    assert graph.dispatch_service._aggregate_locker is locker
    assert graph.predispatch_failure_service._aggregate_locker is locker
    assert graph.acquisition_service._work_item_builder._coordinated_scheduler is scheduler


def test_coordinated_delivery_graph_fails_closed_when_gate_on_dependencies_missing() -> None:
    gates = FeatureGateSet.from_config(
        "full",
        "managed_runtime_worker=true,managed_runtime_coordinated_cutover=true",
    )
    with pytest.raises(InvalidFeatureConfiguration, match="runtime_adapter"):
        _build_coordinated_runtime_delivery_graph(
            uow_factory=lambda: None,
            worker_id="worker-1",
            consumer_name="consumer-1",
            lease_duration=timedelta(minutes=5),
            cancel_deadline_window=timedelta(minutes=5),
            feature_gates=gates,
            runtime_adapter=None,
            worker_runtime_registry=None,
            managed_execution_service=None,
            worker_authority_cohort_resolver=None,
            worker_coordinated_scheduler=None,
            worker_convergence_service=None,
            worker_unknown_service=None,
            runtime_memory_service=None,
            aggregate_locker=None,
        )


def test_worker_container_keeps_deadline_pass_optional_and_bounded() -> None:
    class _DeadlineConsumer:
        def __init__(self) -> None:
            self.calls = 0

        def process_next_deadline(self):
            self.calls += 1
            return SimpleNamespace(operation=object())

    consumer = _DeadlineConsumer()
    container = WorkerContainer(worker=object(), coordinated_deadline_consumer=consumer)

    assert container.process_coordinated_deadline_once() is True
    assert consumer.calls == 1
    assert WorkerContainer(worker=object()).process_coordinated_deadline_once() is False


def test_bootstrap_constructs_deadline_recovery_after_runtime_memory() -> None:
    source = (Path(__file__).parents[1] / "src/agentmesh/bootstrap.py").read_text(
        encoding="utf-8"
    )
    memory = source.index("runtime_memory_service = RuntimeMemoryService(")
    deadline = source.index("coordinated_deadline_consumer = None")
    assert memory < deadline
