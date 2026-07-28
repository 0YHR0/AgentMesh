from __future__ import annotations

import json

import pytest

from agentmesh.domain.errors import McpCatalogUnavailable
from agentmesh.integrations.mcp.registry import OfficialMcpRegistryClient


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def test_official_registry_search_marks_direct_and_unsupported_remotes() -> None:
    payload = {
        "servers": [
            {
                "server": {
                    "name": "io.example/docs",
                    "description": "Read documentation",
                    "version": "1.2.3",
                    "repository": {"url": "https://github.com/example/docs"},
                    "remotes": [
                        {"type": "streamable-http", "url": "https://mcp.example.com/mcp"}
                    ],
                }
            },
            {
                "server": {
                    "name": "io.example/custom-auth",
                    "version": "0.1.0",
                    "remotes": [
                        {
                            "type": "streamable-http",
                            "url": "https://mcp.example.com/custom",
                            "headers": [{"name": "X-Workspace-Key", "isRequired": True}],
                        }
                    ],
                }
            },
        ]
    }
    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response(payload)

    values = OfficialMcpRegistryClient(opener=opener).search("docs", limit=2)

    assert "search=docs" in seen["url"]
    assert seen["timeout"] == 8
    assert values[0].runtime_name == "docs"
    assert values[0].endpoint == "https://mcp.example.com/mcp"
    assert values[0].installable is True
    assert values[0].authentication_required is False
    assert values[1].endpoint is None
    assert values[1].installable is False
    assert "headers" in values[1].compatibility_note


def test_official_registry_search_accepts_bearer_but_rejects_dirty_urls() -> None:
    payload = {
        "servers": [
            {
                "server": {
                    "name": "io.example/bearer",
                    "version": "1.0.0",
                    "remotes": [
                        {
                            "type": "streamable-http",
                            "url": "https://mcp.example.com/mcp",
                            "headers": [{"name": "Authorization", "value": "Bearer {token}"}],
                        }
                    ],
                }
            },
            {
                "server": {
                    "name": "io.example/dirty",
                    "version": "1.0.0",
                    "remotes": [
                        {
                            "type": "streamable-http",
                            "url": "https://mcp.example.com/mcp?token=secret",
                        }
                    ],
                }
            },
        ]
    }
    client = OfficialMcpRegistryClient(opener=lambda *args, **kwargs: _Response(payload))

    values = client.search("example")

    assert values[0].authentication_required is True
    assert values[0].installable is True
    assert values[1].installable is False


def test_official_registry_failure_is_redacted() -> None:
    def opener(*args, **kwargs):
        raise OSError("socket included sensitive diagnostics")

    with pytest.raises(McpCatalogUnavailable, match="unavailable") as captured:
        OfficialMcpRegistryClient(opener=opener).search("docs")

    assert "sensitive" not in str(captured.value)


def test_official_registry_rejects_blank_search_without_network_access() -> None:
    def opener(*args, **kwargs):
        raise AssertionError("blank search must not reach the Registry")

    with pytest.raises(ValueError, match="bounds"):
        OfficialMcpRegistryClient(opener=opener).search("   ")
