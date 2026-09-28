from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import IntegrityError

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import AgentRegistryConflict, InvalidAgentVersion
from agentmesh.domain.model_connections import ModelConnection, validate_endpoint

ENV_REFERENCE = re.compile(
    r"^(AGENTMESH_MODEL_API_KEY_[A-Z0-9_]{1,100}|AGENTMESH_OPENAI_API_KEY|AGENTMESH_DEEPSEEK_API_KEY)$"
)


class ModelConnectionService:
    def __init__(
        self, *, uow_factory: UnitOfWorkFactory, tenant_id: str, encryption_key: str | None
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._fernet = Fernet(encryption_key.encode()) if encryption_key else None

    @property
    def ready(self) -> bool:
        return self._fernet is not None

    def list(self) -> list[dict[str, Any]]:
        with self._uow_factory() as uow:
            return [item.public_dict() for item in uow.model_connections.list(self._tenant_id)]

    def create(self, value: dict[str, Any]) -> dict[str, Any]:
        source, reference, encrypted = self._credential(value.get("credential"))
        try:
            connection = ModelConnection.create(
                tenant_id=self._tenant_id,
                name=value["name"],
                provider=value["provider"],
                model=value.get("model"),
                endpoint=value.get("endpoint"),
                credential_source=source,
                credential_reference=reference,
                encrypted_secret=encrypted,
            )
            with self._uow_factory() as uow:
                if uow.model_connections.get_by_name(self._tenant_id, connection.name):
                    raise AgentRegistryConflict("Model connection name already exists")
                uow.model_connections.add(connection)
                uow.commit()
        except KeyError as exc:
            raise InvalidAgentVersion("Connection name and provider are required") from exc
        except IntegrityError as exc:
            raise AgentRegistryConflict("Model connection name already exists") from exc
        return connection.public_dict()

    def update(self, connection_id: UUID, value: dict[str, Any]) -> dict[str, Any]:
        with self._uow_factory() as uow:
            old = uow.model_connections.get(self._tenant_id, connection_id, for_update=True)
            if old is None:
                raise LookupError(connection_id)
            data = {
                "name": value.get("name", old.name),
                "provider": value.get("provider", old.provider),
                "model": value.get("model", old.model),
                "endpoint": value.get("endpoint", old.endpoint),
            }
            if data["provider"] != old.provider:
                raise InvalidAgentVersion("Provider cannot be changed; create a new connection")
            duplicate = uow.model_connections.get_by_name(self._tenant_id, data["name"])
            if duplicate is not None and duplicate.id != connection_id:
                raise AgentRegistryConflict("Model connection name already exists")
            source, reference, encrypted = (
                old.credential_source,
                old.credential_reference,
                old.encrypted_secret,
            )
            if "credential" in value:
                source, reference, encrypted = self._credential(value["credential"])
            if source == "encrypted" and encrypted is None:
                raise InvalidAgentVersion("A credential is required")
            data["endpoint"] = validate_endpoint(data["provider"], data["endpoint"])
            if not data["name"].strip() or len(data["name"]) > 128:
                raise InvalidAgentVersion("Connection name must be between 1 and 128 characters")
            if not data["model"].strip() or len(data["model"]) > 128:
                raise InvalidAgentVersion("Model name must be between 1 and 128 characters")
            changed = replace(
                old,
                **data,
                credential_source=source,
                credential_reference=reference,
                encrypted_secret=encrypted,
                revision=old.revision + 1,
                updated_at=datetime.now(timezone.utc),
            )
            uow.model_connections.save(changed)
            try:
                uow.commit()
            except IntegrityError as exc:
                raise AgentRegistryConflict("Model connection name already exists") from exc
        return changed.public_dict()

    def disable(self, connection_id: UUID) -> dict[str, Any]:
        with self._uow_factory() as uow:
            old = uow.model_connections.get(self._tenant_id, connection_id, for_update=True)
            if old is None:
                raise LookupError(connection_id)
            changed = replace(
                old, enabled=False, revision=old.revision + 1, updated_at=datetime.now(timezone.utc)
            )
            uow.model_connections.save(changed)
            uow.commit()
        return changed.public_dict()

    def resolve_secret(self, connection: ModelConnection) -> str:
        if connection.credential_source == "environment":
            value = os.environ.get(connection.credential_reference)
            if not value or not value.strip():
                raise InvalidAgentVersion("Model connection environment credential is unavailable")
            return value
        if self._fernet is None or connection.encrypted_secret is None:
            raise InvalidAgentVersion(
                "Model connection encryption key or credential is unavailable"
            )
        try:
            return self._fernet.decrypt(connection.encrypted_secret).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise InvalidAgentVersion("Model connection credential cannot be decrypted") from exc

    def get(self, connection_id: UUID) -> ModelConnection | None:
        with self._uow_factory() as uow:
            return uow.model_connections.get(self._tenant_id, connection_id)

    def test(self, connection_id: UUID) -> dict[str, Any]:
        connection = self.get(connection_id)
        if connection is None:
            raise LookupError(connection_id)
        if not connection.enabled:
            return {"ok": False, "message": "Connection is disabled"}
        try:
            key = self.resolve_secret(connection)
            self._test_provider(connection, key)
        except Exception:
            # Provider errors may echo credentials or submitted content; never forward them.
            return {
                "ok": False,
                "message": "Provider test failed; verify the connection settings and key",
            }
        return {"ok": True, "message": "Provider connection succeeded"}

    @staticmethod
    def _test_provider(connection: ModelConnection, key: str) -> None:
        if connection.provider == "openai":
            payload = {
                "model": connection.model,
                "input": "Reply with OK.",
                "max_output_tokens": 128,
                "store": False,
            }
        else:
            payload = {
                "model": connection.model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 128,
                "stream": False,
                "thinking": {"type": "disabled"},
            }
        request = Request(
            connection.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = build_opener(HTTPSHandler(), NoRedirect())
        try:
            with opener.open(request, timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError("provider rejected request")
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    raise RuntimeError("provider test response is too large")
            value = json.loads(raw)
            if connection.provider == "openai":
                valid = (
                    isinstance(value, dict)
                    and isinstance(value.get("id"), str)
                    and isinstance(value.get("output"), list)
                    and value.get("status") in {"completed", "incomplete"}
                )
            else:
                choices = value.get("choices") if isinstance(value, dict) else None
                valid = (
                    isinstance(choices, list)
                    and bool(choices)
                    and isinstance(choices[0], dict)
                    and isinstance(choices[0].get("message"), dict)
                )
            if not valid:
                raise RuntimeError("provider returned an unexpected response")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Do not surface provider response bodies (they can reflect request data).
            raise RuntimeError("provider test failed") from exc

    def _credential(self, value: Any) -> tuple[str, str, bytes | None]:
        if not isinstance(value, dict):
            raise InvalidAgentVersion("Credential is required")
        if value.get("type") == "api_key":
            key = value.get("value")
            if not isinstance(key, str) or not key.strip() or len(key) > 4096:
                raise InvalidAgentVersion(
                    "API Key must be a non-empty string no longer than 4096 characters"
                )
            if self._fernet is None:
                raise InvalidAgentVersion("Model connection encryption is not configured")
            return "encrypted", "encrypted", self._fernet.encrypt(key.strip().encode())
        if value.get("type") == "environment":
            name = value.get("name")
            if not isinstance(name, str) or not ENV_REFERENCE.fullmatch(name):
                raise InvalidAgentVersion(
                    "Environment reference must use AGENTMESH_MODEL_API_KEY_*"
                )
            return "environment", name, None
        raise InvalidAgentVersion("Credential type must be api_key or environment")
