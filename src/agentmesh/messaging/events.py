from __future__ import annotations

import json
from dataclasses import dataclass

from redis import Redis

from agentmesh.domain.messaging import MessageEnvelope


@dataclass(frozen=True)
class DomainStreamEvent:
    stream_id: str
    envelope: MessageEnvelope


@dataclass(frozen=True)
class DomainStreamBatch:
    cursor: str
    events: tuple[DomainStreamEvent, ...]


class RedisDomainEventStream:
    """Read metadata-bearing domain events without claiming or mutating the Stream."""

    def __init__(self, redis_client: Redis, stream_name: str) -> None:
        self._redis = redis_client
        self._stream_name = stream_name

    def read(self, after_id: str, *, block_ms: int, count: int) -> DomainStreamBatch:
        rows = self._redis.xread({self._stream_name: after_id}, block=block_ms, count=count)
        cursor = after_id
        events: list[DomainStreamEvent] = []
        for _stream, entries in rows:
            for stream_id, fields in entries:
                cursor = str(stream_id)
                raw_envelope = fields.get("envelope")
                if not isinstance(raw_envelope, str):
                    continue
                try:
                    envelope = MessageEnvelope.from_dict(json.loads(raw_envelope))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                events.append(DomainStreamEvent(stream_id=cursor, envelope=envelope))
        return DomainStreamBatch(cursor=cursor, events=tuple(events))
