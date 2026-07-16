from __future__ import annotations

import json
import logging
from typing import Any

from redis import Redis
from redis.exceptions import ResponseError

from agentmesh.application.services import RunExecutionService
from agentmesh.domain.errors import InvalidMessage, RunLeaseUnavailable
from agentmesh.domain.messaging import MessageEnvelope

logger = logging.getLogger(__name__)


class RedisRunWorker:
    def __init__(
        self,
        *,
        redis_client: Redis,
        execution_service: RunExecutionService,
        stream_name: str,
        group_name: str,
        consumer_id: str,
        dead_letter_stream: str,
        block_ms: int,
        pending_idle_ms: int,
    ) -> None:
        self._redis = redis_client
        self._execution_service = execution_service
        self._stream_name = stream_name
        self._group_name = group_name
        self._consumer_id = consumer_id
        self._dead_letter_stream = dead_letter_stream
        self._block_ms = block_ms
        self._pending_idle_ms = pending_idle_ms

    def ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(
                self._stream_name,
                self._group_name,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def run_once(self) -> int:
        self.ensure_group()
        messages = self._claim_stale()
        if not messages:
            response = self._redis.xreadgroup(
                self._group_name,
                self._consumer_id,
                {self._stream_name: ">"},
                count=1,
                block=self._block_ms,
            )
            messages = self._flatten(response)

        processed = 0
        for message_id, fields in messages:
            if self._process(message_id, fields):
                processed += 1
        return processed

    def _claim_stale(self) -> list[tuple[str, dict[str, Any]]]:
        response = self._redis.xautoclaim(
            self._stream_name,
            self._group_name,
            self._consumer_id,
            min_idle_time=self._pending_idle_ms,
            start_id="0-0",
            count=1,
        )
        if not response or len(response) < 2:
            return []
        return [(str(message_id), dict(fields)) for message_id, fields in response[1]]

    @staticmethod
    def _flatten(response: Any) -> list[tuple[str, dict[str, Any]]]:
        messages: list[tuple[str, dict[str, Any]]] = []
        for _stream, stream_messages in response or []:
            messages.extend(
                (str(message_id), dict(fields)) for message_id, fields in stream_messages
            )
        return messages

    def _process(self, message_id: str, fields: dict[str, Any]) -> bool:
        raw_envelope = fields.get("envelope")
        try:
            if not isinstance(raw_envelope, str):
                raise InvalidMessage("Redis message is missing its envelope field")
            envelope = self._decode_envelope(raw_envelope)
            self._execution_service.process(envelope)
        except InvalidMessage as exc:
            self._redis.xadd(
                self._dead_letter_stream,
                {
                    "source_stream": self._stream_name,
                    "source_message_id": message_id,
                    "error": str(exc),
                    "envelope": raw_envelope or "",
                },
            )
        except RunLeaseUnavailable:
            logger.info("Run message %s is still owned by an active attempt", message_id)
            return False
        except Exception:
            logger.exception("Run message %s failed and remains pending", message_id)
            return False

        self._redis.xack(self._stream_name, self._group_name, message_id)
        return True

    @staticmethod
    def _decode_envelope(raw_envelope: str) -> MessageEnvelope:
        try:
            value = json.loads(raw_envelope)
            if not isinstance(value, dict):
                raise TypeError("message envelope must be a JSON object")
            return MessageEnvelope.from_dict(value)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise InvalidMessage(f"Invalid message envelope: {exc}") from exc
