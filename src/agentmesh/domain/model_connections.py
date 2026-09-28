from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidAgentVersion

PROVIDER_DEFAULTS = {
    "openai": ("https://api.openai.com/v1/responses", "gpt-5.6-terra"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash"),
}


@dataclass(frozen=True)
class ModelConnection:
    id: UUID
    tenant_id: str
    name: str
    provider: str
    model: str
    endpoint: str
    credential_source: str
    credential_reference: str
    encrypted_secret: bytes | None
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        name: str,
        provider: str,
        model: str | None,
        endpoint: str | None,
        credential_source: str,
        credential_reference: str,
        encrypted_secret: bytes | None,
    ) -> ModelConnection:
        provider = provider.strip().lower()
        if provider not in PROVIDER_DEFAULTS:
            raise InvalidAgentVersion("Model provider must be openai or deepseek")
        default_endpoint, default_model = PROVIDER_DEFAULTS[provider]
        endpoint = validate_endpoint(provider, endpoint or default_endpoint)
        normalized_model = (model or default_model).strip()
        if not normalized_model or len(normalized_model) > 128:
            raise InvalidAgentVersion("Model name must be between 1 and 128 characters")
        name = name.strip()
        if not name or len(name) > 128:
            raise InvalidAgentVersion("Connection name must be between 1 and 128 characters")
        if credential_source not in {"encrypted", "environment"}:
            raise InvalidAgentVersion("Credential source must be api_key or environment")
        now = datetime.now(timezone.utc)
        return cls(
            uuid4(),
            tenant_id,
            name,
            provider,
            normalized_model,
            endpoint,
            credential_source,
            credential_reference,
            encrypted_secret,
            True,
            1,
            now,
            now,
        )

    def public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "credential_source": "api_key"
            if self.credential_source == "encrypted"
            else "environment",
            "has_credential": bool(self.encrypted_secret or self.credential_reference),
            "enabled": self.enabled,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def validate_endpoint(provider: str, endpoint: str) -> str:
    # Fixed provider domains avoid SSRF and authorization-header forwarding to user URLs.
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(endpoint.strip())
        port = parsed.port
    except ValueError as exc:
        raise InvalidAgentVersion(
            "Endpoint must use the provider's official HTTPS API host"
        ) from exc
    allowed = {"openai": {"api.openai.com"}, "deepseek": {"api.deepseek.com"}}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed.get(provider, set())
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidAgentVersion("Endpoint must use the provider's official HTTPS API host")
    expected_paths = {
        "openai": {"/v1/responses"},
        "deepseek": {"/v1/chat/completions", "/chat/completions"},
    }
    if parsed.path.rstrip("/") not in expected_paths[provider]:
        raise InvalidAgentVersion("Endpoint path is not supported for this provider")
    return endpoint.strip().rstrip("/")
