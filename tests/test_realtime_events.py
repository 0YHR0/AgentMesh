import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.api.event_routes import _encode_sse, _event_iterator
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.features import FeatureGateSet
from agentmesh.messaging.events import (
    DomainStreamBatch,
    DomainStreamEvent,
    RedisDomainEventStream,
)


class ScriptedRequest:
    def __init__(self) -> None:
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > 1


class ScriptedEventStream:
    def __init__(self, batch: DomainStreamBatch) -> None:
        self.batch = batch

    def read(self, after_id: str, *, block_ms: int, count: int) -> DomainStreamBatch:
        assert after_id == "$"
        assert block_ms == 15_000
        assert count == 100
        return self.batch


def _envelope(tenant_id: str, schema_name: str) -> MessageEnvelope:
    return MessageEnvelope.domain_event(
        schema_name=schema_name,
        tenant_id=tenant_id,
        aggregate_id=uuid4(),
        payload={"secret-shaped-field": "must-not-be-forwarded"},
    )


def test_redis_domain_stream_skips_invalid_entries_and_advances_cursor() -> None:
    valid = _envelope("tenant-a", "agentmesh.task.changed")

    class FakeRedis:
        def xread(self, streams, *, block, count):
            assert streams == {"domain": "0-0"}
            assert block == 10
            assert count == 5
            return [
                (
                    "domain",
                    [
                        ("1-0", {"envelope": "not-json"}),
                        ("2-0", {"envelope": json.dumps(valid.to_dict())}),
                        ("3-0", {}),
                    ],
                )
            ]

    batch = RedisDomainEventStream(FakeRedis(), "domain").read(
        "0-0", block_ms=10, count=5
    )

    assert batch.cursor == "3-0"
    assert [event.stream_id for event in batch.events] == ["2-0"]
    assert batch.events[0].envelope.message_id == valid.message_id


def test_sse_iterator_filters_tenant_and_redacts_event_payload() -> None:
    other = _envelope("tenant-b", "agentmesh.artifact.created")
    visible = _envelope("tenant-a", "agentmesh.task.changed")
    batch = DomainStreamBatch(
        cursor="2-0",
        events=(
            DomainStreamEvent("1-0", other),
            DomainStreamEvent("2-0", visible),
        ),
    )

    async def collect() -> list[str]:
        return [
            value
            async for value in _event_iterator(
                ScriptedRequest(),
                stream=ScriptedEventStream(batch),
                tenant_id="tenant-a",
                cursor="$",
            )
        ]

    values = asyncio.run(collect())

    assert "event: ready" in values[0]
    assert len(values) == 2
    assert "event: domain" in values[1]
    assert str(visible.message_id) in values[1]
    assert str(other.message_id) not in values[1]
    assert "secret-shaped-field" not in values[1]


def test_sse_encoding_is_resumable_and_compact() -> None:
    encoded = _encode_sse(
        "unavailable",
        "42-0",
        {"retry": True, "at": datetime.now(timezone.utc).isoformat()},
        retry_ms=3_000,
    )

    assert encoded.startswith("id: 42-0\nevent: unavailable\nretry: 3000\ndata: ")
    assert encoded.endswith("\n\n")


def test_realtime_endpoint_fails_closed_when_disabled_or_unavailable(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as client:
        unavailable = client.get("/api/v1/events")
        assert unavailable.status_code == 503

    disabled_container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(disabled_container)) as client:
        disabled = client.get("/api/v1/events")
        assert disabled.status_code == 403
        assert disabled.json()["code"] == "feature_disabled"
