from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from agentmesh.domain.errors import McpCatalogUnavailable

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"


@dataclass(frozen=True)
class McpRegistryCandidate:
    registry_name: str
    runtime_name: str
    description: str
    version: str
    repository_url: str | None
    endpoint: str | None
    authentication_required: bool
    installable: bool
    compatibility_note: str


class OfficialMcpRegistryClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 8,
        max_response_bytes: int = 1_048_576,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    def search(self, query: str, *, limit: int = 20) -> list[McpRegistryCandidate]:
        normalized = query.strip()
        if not normalized or len(normalized) > 100 or not 1 <= limit <= 50:
            raise ValueError("MCP Registry search bounds are invalid")
        url = f"{REGISTRY_URL}?{urlencode({'search': normalized, 'limit': limit})}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AgentMesh/0.1 MCP Catalog",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read(self._max_response_bytes + 1)
        except Exception as exc:
            raise McpCatalogUnavailable("Official MCP Registry is unavailable") from exc
        if len(body) > self._max_response_bytes:
            raise McpCatalogUnavailable("Official MCP Registry response exceeded the limit")
        try:
            payload = json.loads(body)
            entries = payload["servers"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise McpCatalogUnavailable("Official MCP Registry returned invalid JSON") from exc
        if not isinstance(entries, list):
            raise McpCatalogUnavailable("Official MCP Registry returned an invalid server list")
        return [self._candidate(entry) for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _candidate(entry: dict[str, Any]) -> McpRegistryCandidate:
        server = entry.get("server") if isinstance(entry.get("server"), dict) else {}
        registry_name = str(server.get("name") or "").strip()[:255]
        remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
        endpoint = None
        authentication_required = False
        compatibility_note = "No Streamable HTTP endpoint is published."
        for remote in remotes:
            if not isinstance(remote, dict) or remote.get("type") != "streamable-http":
                continue
            candidate_url = str(remote.get("url") or "").strip()
            parsed = urlsplit(candidate_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                continue
            headers = remote.get("headers") if isinstance(remote.get("headers"), list) else []
            names = {
                str(header.get("name") or "").strip().lower()
                for header in headers
                if isinstance(header, dict)
            }
            if names - {"authorization"}:
                compatibility_note = "Requires headers not supported by the current broker."
                continue
            endpoint = candidate_url
            authentication_required = bool(headers)
            compatibility_note = (
                "Bearer credential setup is required before discovery."
                if authentication_required
                else "Ready for bounded anonymous discovery."
            )
            break
        repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
        runtime_name = registry_name.rsplit("/", 1)[-1] or "mcp-server"
        return McpRegistryCandidate(
            registry_name=registry_name,
            runtime_name=runtime_name[:128],
            description=str(server.get("description") or "").strip()[:2_000],
            version=str(server.get("version") or "0.1.0").strip()[:128],
            repository_url=(
                str(repository.get("url")).strip()[:512] if repository.get("url") else None
            ),
            endpoint=endpoint,
            authentication_required=authentication_required,
            installable=endpoint is not None,
            compatibility_note=compatibility_note,
        )
