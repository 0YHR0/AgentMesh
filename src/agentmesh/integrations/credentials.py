from __future__ import annotations

import os

from agentmesh.domain.credentials import SecretProvider, SecretReference
from agentmesh.domain.errors import CredentialProviderUnavailable


class EnvironmentSecretValueProvider:
    """Resolve an environment reference without copying its value into durable state."""

    def resolve(self, reference: SecretReference) -> str:
        if reference.provider is not SecretProvider.ENVIRONMENT:
            raise CredentialProviderUnavailable("Secret provider is not configured")
        value = os.getenv(reference.external_key)
        if value is None or not value.strip():
            raise CredentialProviderUnavailable("Referenced environment credential is unavailable")
        if "\r" in value or "\n" in value or len(value) > 16_384:
            raise CredentialProviderUnavailable("Referenced environment credential is invalid")
        return value
