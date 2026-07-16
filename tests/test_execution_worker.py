import pytest

from agentmesh.domain.errors import InvalidMessage
from agentmesh.workers.execution import RedisRunWorker


def test_worker_rejects_malformed_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("not-json")


def test_worker_rejects_non_object_envelope() -> None:
    with pytest.raises(InvalidMessage):
        RedisRunWorker._decode_envelope("[]")
