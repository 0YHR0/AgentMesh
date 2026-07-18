from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from agentmesh.domain.errors import AuthenticationFailed, InvalidIdentityConfiguration


@dataclass(frozen=True)
class VerifiedOidcIdentity:
    issuer: str
    subject: str


class OidcJwtVerifier:
    """Verify OIDC access tokens without trusting authorization claims from the IdP."""

    def __init__(self, *, issuer: str, audience: str, cache_seconds: int = 300) -> None:
        self.issuer = issuer.strip().rstrip("/")
        self.audience = audience.strip()
        if not self.issuer.startswith("https://") or not self.audience:
            raise InvalidIdentityConfiguration(
                "OIDC issuer must use HTTPS and audience is required"
            )
        self._cache_seconds = cache_seconds
        self._jwks: PyJWKClient | None = None

    def _jwks_client(self) -> PyJWKClient:
        if self._jwks is not None:
            return self._jwks
        discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        try:
            with urlopen(discovery_url, timeout=5) as response:  # noqa: S310 - configured HTTPS URL
                document = json.loads(response.read(262_145))
            discovered_issuer = str(document["issuer"]).rstrip("/")
            jwks_uri = str(document["jwks_uri"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InvalidIdentityConfiguration("OIDC discovery failed") from exc
        if discovered_issuer != self.issuer:
            raise InvalidIdentityConfiguration("OIDC discovery issuer mismatch")
        parsed = urlparse(jwks_uri)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
            raise InvalidIdentityConfiguration("OIDC jwks_uri must be an HTTPS URL")
        self._jwks = PyJWKClient(
            jwks_uri,
            cache_jwk_set=True,
            lifespan=self._cache_seconds,
        )
        return self._jwks

    def verify(self, token: str) -> VerifiedOidcIdentity:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = str(header.get("alg", ""))
            if algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
                raise AuthenticationFailed("OIDC token uses an unsupported algorithm")
            key = self._jwks_client().get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
            subject = str(claims["sub"]).strip()
            if not subject:
                raise AuthenticationFailed("OIDC subject is missing")
            return VerifiedOidcIdentity(issuer=self.issuer, subject=subject)
        except AuthenticationFailed:
            raise
        except (PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationFailed("OIDC token verification failed") from exc
