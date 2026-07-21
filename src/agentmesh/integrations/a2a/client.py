from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from agentmesh.application.ports import AgentCardFetchResult
from agentmesh.domain.credentials import CredentialMaterial
from agentmesh.domain.errors import A2ATransportFailure

Resolver = Callable[..., list[tuple[Any, ...]]]


class PinnedHttpsA2AClient:
    """Minimal A2A 1.0 HTTP+JSON client with public-address pinning and no redirects."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_request_bytes: int = 65_536,
        max_response_bytes: int = 262_144,
        resolver: Resolver = socket.getaddrinfo,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._resolver = resolver
        self._socket_factory = socket_factory
        if ssl_context is None:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        self._ssl_context = ssl_context
        self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_context.verify_mode = ssl.CERT_REQUIRED
        self._ssl_context.check_hostname = True

    def fetch_agent_card(
        self, *, discovery_url: str, source_etag: str | None = None
    ) -> AgentCardFetchResult:
        parsed = urlsplit(discovery_url)
        if parsed.path != "/.well-known/agent-card.json" or parsed.query:
            raise A2ATransportFailure(
                "A2A discovery URL must use the standard well-known path",
                request_may_have_been_sent=False,
            )
        if source_etag and ("\r" in source_etag or "\n" in source_etag or len(source_etag) > 512):
            raise A2ATransportFailure(
                "A2A discovery ETag is invalid", request_may_have_been_sent=False
            )
        extra_headers = {"If-None-Match": source_etag} if source_etag else {}
        response = self._request_response(
            method="GET",
            url=discovery_url,
            protocol_version="1.0",
            body=None,
            credential=None,
            extra_headers=extra_headers,
            allow_not_modified=True,
        )
        etag = response.headers.get("etag")
        if etag and ("\r" in etag or "\n" in etag or len(etag) > 512):
            raise A2ATransportFailure(
                "A2A discovery response ETag is invalid", request_may_have_been_sent=True
            )
        max_age = _cache_max_age(response.headers.get("cache-control"))
        return AgentCardFetchResult(
            card=response.value,
            source_etag=etag or source_etag,
            cache_max_age_seconds=max_age,
            not_modified=response.status == 304,
        )

    def send_message(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        message: dict[str, Any],
        accepted_output_modes: tuple[str, ...],
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]:
        suffix = (
            f"/{quote(endpoint_tenant, safe='')}/message:send"
            if endpoint_tenant
            else "/message:send"
        )
        payload: dict[str, Any] = {
            "message": message,
            "configuration": {
                "acceptedOutputModes": list(accepted_output_modes),
                "returnImmediately": True,
            },
        }
        if endpoint_tenant:
            payload["tenant"] = endpoint_tenant
        return self._request(
            method="POST",
            url=_operation_url(endpoint_url, suffix),
            protocol_version=protocol_version,
            body=payload,
            credential=credential,
        )

    def get_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]:
        prefix = f"/{quote(endpoint_tenant, safe='')}" if endpoint_tenant else ""
        suffix = f"{prefix}/tasks/{quote(remote_task_id, safe='')}"
        return self._request(
            method="GET",
            url=_operation_url(endpoint_url, suffix),
            protocol_version=protocol_version,
            body=None,
            credential=credential,
        )

    def cancel_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
        metadata: dict[str, Any],
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]:
        prefix = f"/{quote(endpoint_tenant, safe='')}" if endpoint_tenant else ""
        suffix = f"{prefix}/tasks/{quote(remote_task_id, safe='')}:cancel"
        payload: dict[str, Any] = {"id": remote_task_id, "metadata": metadata}
        if endpoint_tenant:
            payload["tenant"] = endpoint_tenant
        return self._request(
            method="POST",
            url=_operation_url(endpoint_url, suffix),
            protocol_version=protocol_version,
            body=payload,
            credential=credential,
            non_success_may_have_been_sent=True,
        )

    def _request(
        self,
        *,
        method: str,
        url: str,
        protocol_version: str,
        body: dict[str, Any] | None,
        credential: CredentialMaterial | None,
        non_success_may_have_been_sent: bool = False,
    ) -> dict[str, Any]:
        return (
            self._request_response(
                method=method,
                url=url,
                protocol_version=protocol_version,
                body=body,
                credential=credential,
                non_success_may_have_been_sent=non_success_may_have_been_sent,
            ).value
            or {}
        )

    def _request_response(
        self,
        *,
        method: str,
        url: str,
        protocol_version: str,
        body: dict[str, Any] | None,
        credential: CredentialMaterial | None,
        extra_headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
        non_success_may_have_been_sent: bool = False,
    ) -> _JsonResponse:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
        ):
            raise A2ATransportFailure(
                "A2A operation URL is not an allowed HTTPS endpoint",
                request_may_have_been_sent=False,
            )
        host = parsed.hostname.lower().rstrip(".")
        port = parsed.port or 443
        addresses = self._public_addresses(host, port)
        request_body = (
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            if body is not None
            else b""
        )
        if len(request_body) > self._max_request_bytes:
            raise A2ATransportFailure(
                "A2A request exceeds the configured size limit",
                request_may_have_been_sent=False,
            )
        raw_socket: socket.socket | None = None
        tls_socket: ssl.SSLSocket | None = None
        send_started = False
        try:
            raw_socket = self._socket_factory((addresses[0], port), self._timeout)
            raw_socket.settimeout(self._timeout)
            ssl_context = self._ssl_context
            # Reassert the security boundary immediately before every handshake. Besides
            # protecting against a caller mutating an injected context after construction,
            # this keeps the protocol restriction on the same data-flow path as wrap_socket.
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.check_hostname = True
            tls_socket = ssl_context.wrap_socket(raw_socket, server_hostname=host)
            raw_socket = None
            target = parsed.path or "/"
            host_header = host if port == 443 else f"{host}:{port}"
            headers = {
                "Host": host_header,
                "Accept": "application/a2a+json, application/json",
                "A2A-Version": protocol_version,
                "Connection": "close",
            }
            for name, value in (extra_headers or {}).items():
                if "\r" in value or "\n" in value or len(value) > 1024:
                    raise A2ATransportFailure(
                        "A2A request header is invalid", request_may_have_been_sent=False
                    )
                headers[name] = value
            if credential is not None:
                if (
                    credential.auth_scheme.lower() != "bearer"
                    or not credential.value
                    or "\r" in credential.value
                    or "\n" in credential.value
                    or len(credential.value) > 16_384
                ):
                    raise A2ATransportFailure(
                        "A2A credential material is invalid",
                        request_may_have_been_sent=False,
                    )
                headers["Authorization"] = f"Bearer {credential.value}"
            if body is not None:
                headers["Content-Type"] = "application/a2a+json"
                headers["Content-Length"] = str(len(request_body))
            request_lines = [f"{method} {target} HTTP/1.1"]
            request_lines.extend(f"{name}: {value}" for name, value in headers.items())
            encoded_request = ("\r\n".join(request_lines) + "\r\n\r\n").encode() + request_body
            send_started = True
            tls_socket.sendall(encoded_request)
            response = http.client.HTTPResponse(tls_socket)
            response.begin()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip()
            response_headers = {
                name: response.getheader(name, "")
                for name in ("ETag", "Cache-Control")
                if response.getheader(name, "")
            }
            response_headers = {key.lower(): value for key, value in response_headers.items()}
            payload = response.read(self._max_response_bytes + 1)
            if len(payload) > self._max_response_bytes:
                raise A2ATransportFailure(
                    "A2A response exceeds the configured size limit",
                    request_may_have_been_sent=True,
                )
            if response.status == 304 and allow_not_modified:
                return _JsonResponse(response.status, None, response_headers)
            if 300 <= response.status < 400:
                raise A2ATransportFailure(
                    "A2A redirects are not allowed",
                    request_may_have_been_sent=True,
                )
            if not 200 <= response.status < 300:
                raise A2ATransportFailure(
                    f"A2A server returned HTTP {response.status}",
                    request_may_have_been_sent=(
                        non_success_may_have_been_sent or response.status >= 500
                    ),
                )
            if content_type not in {"application/a2a+json", "application/json"}:
                raise A2ATransportFailure(
                    "A2A response Content-Type is unsupported",
                    request_may_have_been_sent=True,
                )
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return _JsonResponse(response.status, value, response_headers)
        except A2ATransportFailure:
            raise
        except (
            OSError,
            ssl.SSLError,
            http.client.HTTPException,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise A2ATransportFailure(
                "A2A transport failed",
                request_may_have_been_sent=send_started,
            ) from exc
        finally:
            if tls_socket is not None:
                tls_socket.close()
            if raw_socket is not None:
                raw_socket.close()

    def _public_addresses(self, host: str, port: int) -> list[str]:
        try:
            records = self._resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise A2ATransportFailure(
                "A2A endpoint DNS resolution failed",
                request_may_have_been_sent=False,
            ) from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise A2ATransportFailure(
                "A2A endpoint must resolve exclusively to public IP addresses",
                request_may_have_been_sent=False,
            )
        return addresses


def _operation_url(endpoint_url: str, suffix: str) -> str:
    return endpoint_url.rstrip("/") + suffix


@dataclass(frozen=True)
class _JsonResponse:
    status: int
    value: dict[str, Any] | None
    headers: dict[str, str]


def _cache_max_age(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)\s*(?:,|$)", value, re.IGNORECASE)
    return int(match.group(1)) if match else None
