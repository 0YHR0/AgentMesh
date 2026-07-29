from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agentmesh.domain.errors import InvalidBusinessObject
from agentmesh.domain.tasks import utc_now

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class BusinessObjectTypeStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class ObjectSourceType(str, Enum):
    USER = "USER"
    AGENT = "AGENT"
    IMPORT = "IMPORT"
    SYSTEM = "SYSTEM"


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidBusinessObject(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidBusinessObject(f"{label} must not exceed {maximum} characters")
    return normalized


def _key(value: str, label: str) -> str:
    normalized = _required(value, label, 63).lower()
    if not KEY_PATTERN.fullmatch(normalized):
        raise InvalidBusinessObject(
            f"{label} must start with a letter and contain lowercase letters, "
            "digits, '.', '_' or '-'"
        )
    return normalized


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _validate_schema(schema: dict[str, Any], *, label: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidBusinessObject(f"{label} is not valid JSON Schema: {exc.message}") from exc


def validate_data(
    schema: dict[str, Any], data: dict[str, Any], *, label: str
) -> None:
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path) or "$"
        raise InvalidBusinessObject(f"{label} failed at {path}: {exc.message}") from exc


@dataclass
class BusinessObjectType:
    id: UUID
    company_id: UUID
    key: str
    name: str
    schema_version: int
    json_schema: dict[str, Any]
    lifecycle_definition: dict[str, Any]
    sensitive_fields: list[str]
    ownership_rules: dict[str, Any]
    retention_policy: dict[str, Any]
    status: BusinessObjectTypeStatus
    content_digest: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        key: str,
        name: str,
        schema_version: int,
        json_schema: dict[str, Any],
        lifecycle_definition: dict[str, Any],
        sensitive_fields: list[str] | None = None,
        ownership_rules: dict[str, Any] | None = None,
        retention_policy: dict[str, Any] | None = None,
    ) -> BusinessObjectType:
        if schema_version < 1:
            raise InvalidBusinessObject("Business Object schema version must be positive")
        schema = dict(json_schema)
        _validate_schema(schema, label="Business Object schema")
        lifecycle = cls._normalize_lifecycle(lifecycle_definition)
        schema_properties = set(schema.get("properties", {}))
        for action_key, action in lifecycle["actions"].items():
            unknown_fields = set(action["allowed_update_fields"]) - schema_properties
            if unknown_fields:
                raise InvalidBusinessObject(
                    f"Lifecycle action '{action_key}' updates undeclared fields: "
                    + ", ".join(sorted(unknown_fields))
                )
        sensitive = sorted({_key(item, "Sensitive field") for item in sensitive_fields or []})
        properties = schema.get("properties", {})
        unknown_sensitive = set(sensitive) - set(properties)
        if unknown_sensitive:
            raise InvalidBusinessObject(
                "Sensitive fields are not declared schema properties: "
                + ", ".join(sorted(unknown_sensitive))
            )
        now = utc_now()
        value = cls(
            id=uuid4(),
            company_id=company_id,
            key=_key(key, "Business Object Type key"),
            name=_required(name, "Business Object Type name", 160),
            schema_version=schema_version,
            json_schema=schema,
            lifecycle_definition=lifecycle,
            sensitive_fields=sensitive,
            ownership_rules=dict(ownership_rules or {}),
            retention_policy=dict(retention_policy or {}),
            status=BusinessObjectTypeStatus.DRAFT,
            content_digest="",
            created_at=now,
            updated_at=now,
        )
        value.content_digest = value.calculate_digest()
        return value

    @staticmethod
    def _normalize_lifecycle(value: dict[str, Any]) -> dict[str, Any]:
        lifecycle = dict(value)
        states = lifecycle.get("states")
        initial = lifecycle.get("initial_state")
        actions = lifecycle.get("actions")
        if (
            not isinstance(states, list)
            or not states
            or not all(isinstance(state, str) and state.strip() for state in states)
        ):
            raise InvalidBusinessObject("Lifecycle states must be a non-empty string list")
        normalized_states = [state.strip().upper() for state in states]
        if len(set(normalized_states)) != len(normalized_states):
            raise InvalidBusinessObject("Lifecycle states must be unique")
        if not isinstance(initial, str) or initial.strip().upper() not in normalized_states:
            raise InvalidBusinessObject("Lifecycle initial_state must be a declared state")
        if not isinstance(actions, dict):
            raise InvalidBusinessObject("Lifecycle actions must be an object")
        normalized_actions: dict[str, Any] = {}
        for raw_key, raw_action in actions.items():
            action_key = _key(str(raw_key), "Lifecycle action key")
            if not isinstance(raw_action, dict):
                raise InvalidBusinessObject(f"Lifecycle action '{action_key}' must be an object")
            sources = raw_action.get("from")
            target = raw_action.get("to")
            if (
                not isinstance(sources, list)
                or not sources
                or any(str(source).upper() not in normalized_states for source in sources)
            ):
                raise InvalidBusinessObject(
                    f"Lifecycle action '{action_key}' has invalid source states"
                )
            if not isinstance(target, str) or target.upper() not in normalized_states:
                raise InvalidBusinessObject(
                    f"Lifecycle action '{action_key}' has an invalid target state"
                )
            input_schema = dict(
                raw_action.get(
                    "input_schema",
                    {"type": "object", "additionalProperties": False},
                )
            )
            _validate_schema(
                input_schema, label=f"Lifecycle action '{action_key}' input schema"
            )
            allowed_fields = raw_action.get("allowed_update_fields", [])
            if not isinstance(allowed_fields, list) or not all(
                isinstance(field, str) for field in allowed_fields
            ):
                raise InvalidBusinessObject(
                    f"Lifecycle action '{action_key}' allowed_update_fields must be a list"
                )
            normalized_actions[action_key] = {
                "from": [str(source).upper() for source in sources],
                "to": target.upper(),
                "input_schema": input_schema,
                "allowed_update_fields": sorted(set(allowed_fields)),
                "required_evidence": bool(raw_action.get("required_evidence", False)),
                "side_effect_class": str(
                    raw_action.get("side_effect_class", "NONE")
                ).upper(),
                "required_position_keys": sorted(
                    set(raw_action.get("required_position_keys", []))
                ),
                "required_capabilities": sorted(
                    set(raw_action.get("required_capabilities", []))
                ),
            }
        return {
            "states": normalized_states,
            "initial_state": initial.strip().upper(),
            "actions": normalized_actions,
        }

    def calculate_digest(self) -> str:
        return _digest(
            {
                "company_id": str(self.company_id),
                "key": self.key,
                "name": self.name,
                "schema_version": self.schema_version,
                "json_schema": self.json_schema,
                "lifecycle_definition": self.lifecycle_definition,
                "sensitive_fields": self.sensitive_fields,
                "ownership_rules": self.ownership_rules,
                "retention_policy": self.retention_policy,
            }
        )

    def publish(self) -> None:
        if self.status is not BusinessObjectTypeStatus.DRAFT:
            raise InvalidBusinessObject(
                f"Cannot publish Business Object Type from {self.status.value}"
            )
        self.status = BusinessObjectTypeStatus.PUBLISHED
        self.updated_at = utc_now()

    def deprecate(self) -> None:
        if self.status is not BusinessObjectTypeStatus.PUBLISHED:
            raise InvalidBusinessObject(
                f"Cannot deprecate Business Object Type from {self.status.value}"
            )
        self.status = BusinessObjectTypeStatus.DEPRECATED
        self.updated_at = utc_now()

    @property
    def initial_state(self) -> str:
        return str(self.lifecycle_definition["initial_state"])

    def action(self, key: str) -> dict[str, Any]:
        action = self.lifecycle_definition["actions"].get(key.strip().lower())
        if action is None:
            raise InvalidBusinessObject(f"Unknown Business Object action '{key}'")
        return dict(action)


@dataclass
class BusinessObject:
    id: UUID
    company_id: UUID
    type_id: UUID
    external_ref: str | None
    current_revision: int
    lifecycle_state: str
    owner_position_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        type_id: UUID,
        initial_state: str,
        external_ref: str | None,
        owner_position_id: UUID | None,
    ) -> BusinessObject:
        now = utc_now()
        normalized_ref = external_ref.strip() if external_ref else None
        if normalized_ref and len(normalized_ref) > 255:
            raise InvalidBusinessObject("External reference must not exceed 255 characters")
        return cls(
            id=uuid4(),
            company_id=company_id,
            type_id=type_id,
            external_ref=normalized_ref,
            current_revision=1,
            lifecycle_state=initial_state,
            owner_position_id=owner_position_id,
            created_at=now,
            updated_at=now,
        )

    def apply(self, *, expected_revision: int, target_state: str) -> None:
        if expected_revision != self.current_revision:
            raise InvalidBusinessObject(
                f"Stale Business Object revision: expected {expected_revision}, "
                f"current is {self.current_revision}"
            )
        self.current_revision += 1
        self.lifecycle_state = target_state
        self.updated_at = utc_now()


@dataclass(frozen=True)
class BusinessObjectRevision:
    object_id: UUID
    revision: int
    schema_version: int
    action: str
    data: dict[str, Any]
    data_digest: str
    source_type: ObjectSourceType
    source_id: str | None
    actor: str
    evidence_refs: list[str]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        object_id: UUID,
        revision: int,
        schema_version: int,
        action: str,
        data: dict[str, Any],
        source_type: ObjectSourceType,
        source_id: str | None,
        actor: str,
        evidence_refs: list[str] | None = None,
    ) -> BusinessObjectRevision:
        return cls(
            object_id=object_id,
            revision=revision,
            schema_version=schema_version,
            action=_required(action, "Revision action", 63),
            data=dict(data),
            data_digest=_digest(data),
            source_type=source_type,
            source_id=source_id.strip() if source_id else None,
            actor=_required(actor, "Revision actor", 128),
            evidence_refs=sorted(set(evidence_refs or [])),
            created_at=utc_now(),
        )
