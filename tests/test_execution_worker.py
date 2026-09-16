import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import DeliveryInProgress
from agentmesh.bootstrap import WorkerContainer
from agentmesh.domain.errors import InvalidMessage
from agentmesh.domain.messaging import MessageEnvelope
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
