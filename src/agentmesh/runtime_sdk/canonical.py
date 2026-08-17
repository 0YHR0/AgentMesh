"""Deterministic, dependency-free JSON representation for Runtime DTOs."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

CANONICALIZATION_VERSION = "agentmesh-runtime-v1"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented by the Runtime contract."""


def normalize_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, rejecting ambiguous naive timestamps."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("timestamps must include an RFC 3339 timezone")
    return value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        timestamp = normalize_utc(value).isoformat(timespec="microseconds")
        return timestamp[:-6] + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalizationError("non-finite numbers are not valid canonical JSON")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise CanonicalizationError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data with stable UTF-8 bytes."""

    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, CanonicalizationError):
            raise
        raise CanonicalizationError("value is not canonical JSON") from exc


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return the lower-case, unprefixed SHA-256 digest of canonical bytes."""

    return sha256(canonical_json_bytes(value)).hexdigest()


sha256_digest = canonical_digest
