from pathlib import Path
from types import SimpleNamespace

import pytest

from agentmesh.bootstrap import WorkerContainer
from agentmesh.domain.errors import InvalidMessage
from agentmesh.workers.execution import RedisRunWorker


def test_worker_rejects_malformed_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("not-json")


def test_worker_rejects_non_object_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("[]")


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
