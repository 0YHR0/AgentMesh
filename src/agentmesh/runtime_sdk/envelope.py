from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .canonical import canonical_json_bytes
from .common import (
    API_VERSION,
    RuntimeContractError,
    UnknownMajorVersion,
    _bounded,
    _closed,
    _exact_dict,
    _exact_int,
    _expect_mapping,
    _reject_secrets,
    _text,
    _timestamp,
    _uuid,
)


@dataclass(frozen=True)
class Envelope:
    """Common cross-process envelope used by Runtime commands and events."""

    schema_name: str
    schema_version: int
    message_id: str
    tenant_id: str
    occurred_at: datetime
    producer: str
    actor: str
    correlation_id: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None
    causation_id: str | None = None
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.schema_name, "schema_name", max_bytes=256)
        _exact_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != API_VERSION:
            raise UnknownMajorVersion(
                f"unsupported envelope major version: {self.schema_version!r}"
            )
        for name in ("message_id", "correlation_id"):
            _uuid(getattr(self, name), name)
        if self.causation_id is not None:
            _uuid(self.causation_id, "causation_id")
        _text(self.tenant_id, "tenant_id", max_bytes=256)
        _timestamp(self.occurred_at, "occurred_at")
        _text(self.producer, "producer", max_bytes=256)
        _text(self.actor, "actor", max_bytes=512)
        _exact_dict(self.payload, "payload")
        _exact_dict(self.trace_context, "trace_context")
        _exact_dict(self.extensions, "extensions")
        if self.idempotency_key is not None:
            _text(self.idempotency_key, "idempotency_key", max_bytes=512)
        if self.expires_at is not None:
            _timestamp(self.expires_at, "expires_at")
        for name, value in (
            ("payload", self.payload),
            ("trace_context", self.trace_context),
            ("extensions", self.extensions),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        if len(canonical_json_bytes(self.to_dict())) > 256 * 1024:
            raise RuntimeContractError("envelope exceeds the 256 KiB limit")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "actor": self.actor,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }
        optional = {
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "trace_context": dict(self.trace_context) if self.trace_context else None,
            "expires_at": self.expires_at,
            "extensions": dict(self.extensions) if self.extensions else None,
        }
        result.update({name: value for name, value in optional.items() if value is not None})
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Envelope:
        data = _expect_mapping(value, "envelope")
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "message_id",
                "tenant_id",
                "occurred_at",
                "producer",
                "actor",
                "correlation_id",
                "payload",
                "idempotency_key",
                "causation_id",
                "trace_context",
                "expires_at",
                "extensions",
            },
            "envelope",
        )
        return cls(
            schema_name=_text(data.get("schema_name"), "schema_name", max_bytes=256),
            schema_version=data.get("schema_version"),
            message_id=_uuid(data.get("message_id"), "message_id"),
            tenant_id=_text(data.get("tenant_id"), "tenant_id", max_bytes=256),
            occurred_at=_timestamp(data.get("occurred_at"), "occurred_at")
            or datetime.now().astimezone(),
            producer=_text(data.get("producer"), "producer", max_bytes=256),
            actor=_text(data.get("actor"), "actor", max_bytes=512),
            correlation_id=_uuid(data.get("correlation_id"), "correlation_id"),
            payload=_expect_mapping(data.get("payload"), "payload"),
            idempotency_key=data.get("idempotency_key"),
            causation_id=data.get("causation_id"),
            trace_context=_expect_mapping(data.get("trace_context", {}), "trace_context"),
            expires_at=_timestamp(data.get("expires_at"), "expires_at", required=False),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )
