"""Operator-owned, tenant/group-bound employee bot credentials. No registration or chat ingress."""

import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

_MAX_FILE_BYTES = 65_536


class EmployeeBotCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9_]+$", max_length=128)
    app_secret: SecretStr = Field(repr=False)

    @field_validator("agent_id")
    @classmethod
    def exact_identifier(cls, value: str) -> str:
        if value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Invalid employee identifier")
        return value

    @field_validator("app_secret")
    @classmethod
    def bounded_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or len(raw) > 1024 or any(c.isspace() for c in raw):
            raise ValueError("Invalid bot credential")
        return value


class EmployeeBotFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    tenant_id: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(pattern=r"^oc_[A-Za-z0-9_]+$", max_length=128)
    bots: list[EmployeeBotCredential] = Field(min_length=1, max_length=256)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Invalid schema version")
        return value


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration field")
        result[key] = value
    return result


def load_employee_bot_credentials(
    path: str, *, tenant_id: str, chat_id: str
) -> dict[str, EmployeeBotCredential]:
    """Bound reads and reject ambiguity, wrong destinations and broadly readable POSIX files.

    Validation errors deliberately omit the original inputs, which contain App Secrets.
    Windows operators must use an ACL-protected file; Windows permission bits are not ACLs.
    """
    try:
        with Path(path).open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Not a regular configuration file")
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValueError("Insecure configuration permissions")
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("Configuration too large")
        parsed = json.loads(raw, object_pairs_hook=_unique_json_keys)
        configuration = EmployeeBotFile.model_validate(parsed)
        if configuration.tenant_id != tenant_id or configuration.chat_id != chat_id:
            raise ValueError("Wrong tenant or group")
        credentials = {bot.agent_id: bot for bot in configuration.bots}
        if len(credentials) != len(configuration.bots):
            raise ValueError("Duplicate employee binding")
        if len({bot.app_id for bot in configuration.bots}) != len(configuration.bots):
            raise ValueError("A bot must not impersonate multiple employees")
        return credentials
    except (OSError, ValueError, ValidationError, RecursionError):
        raise ValueError(
            "Invalid Feishu employee bot file: check schema, tenant/group, unique bindings "
            "and private file permissions; credential details withheld"
        ) from None
