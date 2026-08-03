from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from agentmesh.extensions.sdk import ExtensionManifest, InvalidRuntimeExtension

EXTENSION_LOCK_API_VERSION = "0.1"
DEFAULT_LOCK_PATH = Path("extensions.lock")
PACKAGED_LOCK_PATH = Path(__file__).with_name("default.lock")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DISTRIBUTION_SEPARATOR = re.compile(r"[-_.]+")
_TRUST_LEVELS = {"built-in", "verified", "local", "unverified"}


@dataclass(frozen=True)
class LockedExtension:
    identifier: str
    version: str
    trust: Literal["built-in", "verified", "local", "unverified", "unmanaged"]
    source: str | None
    distribution: str | None
    entry_point: str | None
    wheel_sha256: str | None
    required_features: tuple[str, ...]
    required_credentials: tuple[str, ...]
    permissions: tuple[str, ...]
    external_writes_enabled: bool

    @classmethod
    def unmanaged(cls, manifest: ExtensionManifest) -> LockedExtension:
        return cls(
            identifier=manifest.identifier,
            version=manifest.version,
            trust="unmanaged",
            source=None,
            distribution=None,
            entry_point=None,
            wheel_sha256=None,
            required_features=manifest.required_features,
            required_credentials=manifest.required_credentials,
            permissions=manifest.permissions,
            external_writes_enabled=manifest.external_writes_enabled,
        )

    def validate_manifest(self, manifest: ExtensionManifest) -> None:
        comparisons: tuple[tuple[str, object, object], ...] = (
            ("identifier", self.identifier, manifest.identifier),
            ("version", self.version, manifest.version),
            ("required_features", self.required_features, manifest.required_features),
            ("required_credentials", self.required_credentials, manifest.required_credentials),
            ("permissions", self.permissions, manifest.permissions),
            (
                "external_writes_enabled",
                self.external_writes_enabled,
                manifest.external_writes_enabled,
            ),
        )
        for field, expected, actual in comparisons:
            if expected != actual:
                raise InvalidRuntimeExtension(
                    f"Extension '{manifest.identifier}' manifest field '{field}' does not match "
                    "extensions.lock"
                )


class ExtensionLock:
    def __init__(self, entries: tuple[LockedExtension, ...]) -> None:
        self._by_id: dict[str, LockedExtension] = {}
        self._by_entry_point: dict[tuple[str, str], LockedExtension] = {}
        for entry in entries:
            if entry.identifier in self._by_id:
                raise InvalidRuntimeExtension(
                    f"Extension '{entry.identifier}' occurs more than once in extensions.lock"
                )
            self._by_id[entry.identifier] = entry
            if entry.distribution and entry.entry_point:
                key = (_normalize_distribution(entry.distribution), entry.entry_point)
                if key in self._by_entry_point:
                    raise InvalidRuntimeExtension(
                        f"Extension Entry Point '{entry.distribution}:{entry.entry_point}' is "
                        "locked more than once"
                    )
                self._by_entry_point[key] = entry

    @classmethod
    def load(cls, path: Path) -> ExtensionLock:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidRuntimeExtension(f"Cannot read extension lock '{path}': {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"api_version", "extensions"}:
            raise InvalidRuntimeExtension(
                "extensions.lock must contain only 'api_version' and 'extensions'"
            )
        if value["api_version"] != EXTENSION_LOCK_API_VERSION:
            raise InvalidRuntimeExtension(
                f"Unsupported extension lock API version '{value['api_version']}'"
            )
        raw_entries = value["extensions"]
        if not isinstance(raw_entries, list):
            raise InvalidRuntimeExtension("extensions.lock 'extensions' must be a list")
        return cls(tuple(_parse_entry(item) for item in raw_entries))

    def get(self, identifier: str) -> LockedExtension:
        try:
            return self._by_id[identifier]
        except KeyError as exc:
            raise InvalidRuntimeExtension(
                f"Extension '{identifier}' is not present in extensions.lock"
            ) from exc

    def find_entry_point(self, distribution: str, name: str) -> LockedExtension | None:
        return self._by_entry_point.get((_normalize_distribution(distribution), name))

    def list(self) -> tuple[LockedExtension, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))


def load_configured_extension_lock() -> ExtensionLock:
    configured = os.getenv("AGENTMESH_EXTENSION_LOCK_PATH")
    if configured:
        return ExtensionLock.load(Path(configured))
    if DEFAULT_LOCK_PATH.is_file():
        return ExtensionLock.load(DEFAULT_LOCK_PATH)
    return ExtensionLock.load(PACKAGED_LOCK_PATH)


def _parse_entry(value: object) -> LockedExtension:
    fields = {
        "id",
        "version",
        "trust",
        "source",
        "distribution",
        "entry_point",
        "wheel_sha256",
        "required_features",
        "required_credentials",
        "permissions",
        "external_writes_enabled",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidRuntimeExtension(
            "Each extensions.lock entry must contain the complete v0.1 field set"
        )
    identifier = _string(value, "id")
    version = _string(value, "version")
    trust = _string(value, "trust")
    if trust not in _TRUST_LEVELS:
        raise InvalidRuntimeExtension(f"Invalid trust level '{trust}' for extension '{identifier}'")
    source = _optional_string(value, "source")
    distribution = _optional_string(value, "distribution")
    entry_point = _optional_string(value, "entry_point")
    wheel_sha256 = _optional_string(value, "wheel_sha256")
    if trust == "built-in":
        if any(item is not None for item in (distribution, entry_point, wheel_sha256)):
            raise InvalidRuntimeExtension(
                f"Built-in extension '{identifier}' cannot declare wheel metadata"
            )
    else:
        if not all((source, distribution, entry_point, wheel_sha256)):
            raise InvalidRuntimeExtension(
                f"External extension '{identifier}' requires source, distribution, Entry Point, "
                "and wheel SHA-256"
            )
        if not _SHA256.fullmatch(wheel_sha256 or ""):
            raise InvalidRuntimeExtension(
                f"Extension '{identifier}' wheel_sha256 must be 64 lowercase hex characters"
            )
    external_writes = value["external_writes_enabled"]
    if not isinstance(external_writes, bool):
        raise InvalidRuntimeExtension("external_writes_enabled must be a boolean")
    return LockedExtension(
        identifier=identifier,
        version=version,
        trust=cast(Literal["built-in", "verified", "local", "unverified"], trust),
        source=source,
        distribution=distribution,
        entry_point=entry_point,
        wheel_sha256=wheel_sha256,
        required_features=_string_tuple(value, "required_features"),
        required_credentials=_string_tuple(value, "required_credentials"),
        permissions=_string_tuple(value, "permissions"),
        external_writes_enabled=external_writes,
    )


def _string(value: dict[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item.strip():
        raise InvalidRuntimeExtension(f"extensions.lock '{key}' must be a non-empty string")
    return item.strip()


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value[key]
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise InvalidRuntimeExtension(f"extensions.lock '{key}' must be null or a string")
    return item.strip()


def _string_tuple(value: dict[str, Any], key: str) -> tuple[str, ...]:
    item = value[key]
    if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
        raise InvalidRuntimeExtension(f"extensions.lock '{key}' must be a string list")
    result = tuple(item)
    if len(set(result)) != len(result):
        raise InvalidRuntimeExtension(f"extensions.lock '{key}' values must be unique")
    return result


def _normalize_distribution(value: str) -> str:
    return _DISTRIBUTION_SEPARATOR.sub("-", value).lower()
