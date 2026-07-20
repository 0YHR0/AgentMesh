from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

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
        self._ssl_context = ssl_context or ssl.create_default_context()

    def send_message(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        message: dict[str, Any],
        accepted_output_modes: tuple[str, ...],
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
        )

    def get_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
    ) -> dict[str, Any]:
        prefix = f"/{quote(endpoint_tenant, safe='')}" if endpoint_tenant else ""
        suffix = f"{prefix}/tasks/{quote(remote_task_id, safe='')}"
        return self._request(
            method="GET",
            url=_operation_url(endpoint_url, suffix),
            protocol_version=protocol_version,
            body=None,
        )

    def _request(
        self,
        *,
        method: str,
        url: str,
        protocol_version: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
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
            tls_socket = self._ssl_context.wrap_socket(raw_socket, server_hostname=host)
            raw_socket = None
            target = parsed.path or "/"
            host_header = host if port == 443 else f"{host}:{port}"
            headers = {
                "Host": host_header,
                "Accept": "application/a2a+json, application/json",
                "A2A-Version": protocol_version,
                "Connection": "close",
            }
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
            payload = response.read(self._max_response_bytes + 1)
            if len(payload) > self._max_response_bytes:
                raise A2ATransportFailure(
                    "A2A response exceeds the configured size limit",
                    request_may_have_been_sent=True,
                )
            if 300 <= response.status < 400:
                raise A2ATransportFailure(
                    "A2A redirects are not allowed",
                    request_may_have_been_sent=True,
                )
            if not 200 <= response.status < 300:
                raise A2ATransportFailure(
                    f"A2A server returned HTTP {response.status}",
                    request_may_have_been_sent=response.status >= 500,
                )
            if content_type not in {"application/a2a+json", "application/json"}:
                raise A2ATransportFailure(
                    "A2A response Content-Type is unsupported",
                    request_may_have_been_sent=True,
                )
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return value
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
