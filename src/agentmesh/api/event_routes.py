from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature
from agentmesh.messaging.events import RedisDomainEventStream

router = APIRouter(
    prefix="/api/v1",
    tags=["events"],
    dependencies=[
        Depends(require_feature(Feature.REALTIME_EVENTS)),
        Depends(require_permission(Permission.SYSTEM_INSPECT)),
    ],
)

LastEventId = Annotated[
    str | None,
    Header(alias="Last-Event-ID", pattern=r"^(\d+-\d+|\$)$", max_length=64),
]


def get_event_stream(request: Request) -> RedisDomainEventStream:
    stream = request.app.state.container.event_stream
    if stream is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Realtime event Stream is unavailable",
        )
    return stream


EventStreamDependency = Annotated[RedisDomainEventStream, Depends(get_event_stream)]


@router.get("/events")
async def stream_events(
    request: Request,
    principal: PrincipalDependency,
    stream: EventStreamDependency,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    return StreamingResponse(
        _event_iterator(
            request,
            stream=stream,
            tenant_id=principal.tenant_id,
            cursor=last_event_id or "$",
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_iterator(
    request: Request,
    *,
    stream: RedisDomainEventStream,
    tenant_id: str,
    cursor: str,
) -> AsyncIterator[str]:
    yield _encode_sse("ready", cursor, {"cursor": cursor})
    while not await request.is_disconnected():
        try:
            batch = await asyncio.to_thread(stream.read, cursor, block_ms=15_000, count=100)
        except RedisError:
            yield _encode_sse("unavailable", cursor, {"retry": True}, retry_ms=3_000)
            return
        previous_cursor = cursor
        cursor = batch.cursor
        emitted_cursor = previous_cursor
        for item in batch.events:
            if item.envelope.tenant_id != tenant_id:
                continue
            envelope = item.envelope
            yield _encode_sse(
                "domain",
                item.stream_id,
                {
                    "message_id": str(envelope.message_id),
                    "schema_name": envelope.schema_name,
                    "schema_version": envelope.schema_version,
                    "occurred_at": envelope.occurred_at.isoformat(),
                    "correlation_id": str(envelope.correlation_id),
                },
            )
            emitted_cursor = item.stream_id
        if cursor != emitted_cursor or cursor == previous_cursor:
            yield _encode_sse("heartbeat", cursor, {"cursor": cursor})


def _encode_sse(
    event: str,
    event_id: str,
    data: dict[str, Any],
    *,
    retry_ms: int | None = None,
) -> str:
    lines = [f"id: {event_id}", f"event: {event}"]
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"
