"""RFC 8785 JSON Canonicalization Scheme (JCS) for Runtime contracts.

The implementation deliberately does not delegate number rendering to
``json.dumps``. Python's encoder is useful for parsing, but its thresholds and
negative-zero spelling are not the protocol authority. This small encoder
implements the JCS subset required by the JSON data model and rejects values
that cannot be represented by an interoperable IEEE-754 double.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

CANONICALIZATION_VERSION = "agentmesh-runtime-jcs-v1"
MAX_SAFE_INTEGER = 2**53 - 1
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented by RFC 8785 JCS."""


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("timestamps must include an RFC 3339 timezone")
    return value.astimezone(timezone.utc)


def _normalize_unicode(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                raise CanonicalizationError("lone Unicode surrogates are not valid JCS strings")
            result.append(
                chr(0x10000 + ((codepoint - 0xD800) << 10) + ord(value[index + 1]) - 0xDC00)
            )
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise CanonicalizationError("lone Unicode surrogates are not valid JCS strings")
        result.append(value[index])
        index += 1
    return "".join(result)


def _string(value: str) -> str:
    value = _normalize_unicode(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise CanonicalizationError("boolean is not a number")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError("integers outside the IEEE-754 safe range are unsupported")
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError("NaN and Infinity are not valid JCS numbers")
    if value == 0:
        return "0"
    text = repr(value).lower()
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    digits = mantissa.replace(".", "")
    fraction_digits = len(mantissa.partition(".")[2])
    decimal_exponent = exponent - fraction_digits
    digits = digits.lstrip("0") or "0"
    adjusted_exponent = decimal_exponent + len(digits) - 1
    if -6 <= adjusted_exponent < 21:
        point = len(digits) + decimal_exponent
        if point <= 0:
            body = "0." + "0" * (-point) + digits
        elif point >= len(digits):
            body = digits + "0" * (point - len(digits))
        else:
            body = digits[:point] + "." + digits[point:]
        if "." in body:
            body = body.rstrip("0").rstrip(".")
        return sign + body
    coefficient = digits[0]
    tail = digits[1:].rstrip("0")
    if tail:
        coefficient += "." + tail
    exponent_sign = "+" if adjusted_exponent >= 0 else "-"
    return f"{sign}{coefficient}e{exponent_sign}{abs(adjusted_exponent)}"


def _key_sort(value: str) -> bytes:
    return value.encode("utf-16-be", "surrogatepass")


def _prepare(value: Any) -> Any:
    if is_dataclass(value):
        return _prepare(asdict(value))
    if isinstance(value, Enum):
        return _prepare(value.value)
    if isinstance(value, UUID):
        return str(value)
    if type(value) is datetime:
        timestamp = normalize_utc(value).isoformat(timespec="microseconds")
        return timestamp[:-6] + "Z"
    if type(value) is date:
        return value.isoformat()
    if type(value) is str:
        return _normalize_unicode(value)
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise CanonicalizationError("JSON object keys must be strings")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_unicode(key)
            if normalized_key in normalized:
                raise CanonicalizationError("Unicode-normalized object keys must be unique")
            normalized[normalized_key] = _prepare(item)
        return normalized
    if type(value) is list or type(value) is tuple:
        return [_prepare(item) for item in value]
    if value is None or type(value) is bool or type(value) is int or type(value) is float:
        if type(value) is int or type(value) is float:
            _number(value)
        return value
    raise CanonicalizationError(f"unsupported JSON value: {type(value).__name__}")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is str:
        return _string(value)
    if type(value) is int or type(value) is float:
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        members = [_string(key) + ":" + _encode(value[key]) for key in sorted(value, key=_key_sort)]
        return "{" + ",".join(members) + "}"
    raise CanonicalizationError(f"unsupported prepared value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return strict RFC 8785 canonical UTF-8 bytes."""

    try:
        return _encode(_prepare(value)).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("canonical JSON must be valid UTF-8") from exc


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def decode_json(value: str | bytes | bytearray) -> Any:
    """Decode protocol JSON while rejecting duplicates and unsafe numbers."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise CanonicalizationError(f"duplicate JSON object key: {key!r}")
            result[key] = item
        return result

    def parse_int(text: str) -> int:
        number = int(text)
        if abs(number) > MAX_SAFE_INTEGER:
            raise CanonicalizationError("integers outside the IEEE-754 safe range are unsupported")
        return number

    def parse_float(text: str) -> float:
        number = float(text)
        if not math.isfinite(number):
            raise CanonicalizationError("NaN and Infinity are not valid JSON numbers")
        if not _NUMBER.fullmatch(text):
            raise CanonicalizationError("invalid JSON number")
        return number

    try:
        result = json.loads(
            value,
            object_pairs_hook=pairs,
            parse_int=parse_int,
            parse_float=parse_float,
            parse_constant=lambda text: (_ for _ in ()).throw(
                CanonicalizationError(f"invalid JSON constant: {text}")
            ),
        )
        # Validate strings/containers with the same JCS rules as direct
        # construction (notably lone surrogates and safe numeric values).
        canonical_json_bytes(result)
        return result
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise CanonicalizationError("invalid protocol JSON") from exc


def canonical_digest(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


sha256_digest = canonical_digest
