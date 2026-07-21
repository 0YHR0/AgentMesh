import json
import ssl
from uuid import uuid4

import pytest

from agentmesh.domain.credentials import CredentialMaterial
from agentmesh.domain.errors import A2ATransportFailure
from agentmesh.integrations.a2a.client import PinnedHttpsA2AClient


class _Socket:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def settimeout(self, timeout) -> None:
        self.timeout = timeout

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def close(self) -> None:
        self.closed = True


class _TlsContext:
    def __init__(self) -> None:
        self.server_hostname = None
        self.minimum_version = None
        self.verify_mode = None
        self.check_hostname = False

    def wrap_socket(self, sock, *, server_hostname):
        self.server_hostname = server_hostname
        return sock


class _Response:
    status = 200

    def __init__(self, sock) -> None:
        self.sock = sock

    def begin(self) -> None:
        pass

    def getheader(self, name, default=""):
        return "application/a2a+json" if name == "Content-Type" else default

    def read(self, limit):
        return json.dumps(
            {
                "task": {
                    "id": "remote-1",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            }
        ).encode()


class _CardResponse(_Response):
    def getheader(self, name, default=""):
        return {
            "Content-Type": "application/json",
            "ETag": '"card-v2"',
            "Cache-Control": "public, max-age=7200",
        }.get(name, default)

    def read(self, limit):
        return json.dumps({"name": "Remote Agent"}).encode()


class _NotModifiedResponse(_CardResponse):
    status = 304

    def read(self, limit):
        return b""


def test_client_rejects_any_private_dns_answer_before_connecting() -> None:
    connected = False

    def connect(*args):
        nonlocal connected
        connected = True
        return _Socket()

    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ],
        socket_factory=connect,
        ssl_context=_TlsContext(),
    )
    with pytest.raises(A2ATransportFailure, match="public IP") as caught:
        client.get_task(
            endpoint_url="https://peer.example/a2a/v1",
            protocol_version="1.0",
            endpoint_tenant=None,
            remote_task_id="remote-1",
        )
    assert not caught.value.request_may_have_been_sent
    assert not connected


def test_client_pins_tls_host_and_emits_a2a_1_tenant_request(monkeypatch) -> None:
    sock = _Socket()
    tls = _TlsContext()
    monkeypatch.setattr("agentmesh.integrations.a2a.client.http.client.HTTPResponse", _Response)
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: sock,
        ssl_context=tls,
    )
    result = client.send_message(
        endpoint_url="https://peer.example/a2a/v1",
        protocol_version="1.0",
        endpoint_tenant="acme/team",
        message={"messageId": "stable-id", "role": "ROLE_USER", "parts": []},
        accepted_output_modes=("application/json",),
    )

    request = sock.sent.decode()
    assert result["task"]["id"] == "remote-1"
    assert request.startswith("POST /a2a/v1/acme%2Fteam/message:send HTTP/1.1\r\n")
    assert "\r\nA2A-Version: 1.0\r\n" in request
    assert "Authorization:" not in request
    assert '"returnImmediately":true' in request
    assert '"messageId":"stable-id"' in request
    assert tls.server_hostname == "peer.example"
    assert tls.minimum_version is ssl.TLSVersion.TLSv1_2
    assert tls.verify_mode is ssl.CERT_REQUIRED
    assert tls.check_hostname
    assert sock.closed


def test_client_adds_brokered_bearer_only_to_http_header(monkeypatch) -> None:
    sock = _Socket()
    monkeypatch.setattr("agentmesh.integrations.a2a.client.http.client.HTTPResponse", _Response)
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: sock,
        ssl_context=_TlsContext(),
    )
    client.get_task(
        endpoint_url="https://peer.example/a2a/v1",
        protocol_version="1.0",
        endpoint_tenant=None,
        remote_task_id="remote-1",
        credential=CredentialMaterial(
            lease_id=uuid4(), auth_scheme="Bearer", value="broker-only-token"
        ),
    )
    request = sock.sent.decode()
    headers, _ = request.split("\r\n\r\n", 1)
    assert "Authorization: Bearer broker-only-token" in headers


def test_client_emits_a2a_cancel_task_request(monkeypatch) -> None:
    sock = _Socket()
    monkeypatch.setattr("agentmesh.integrations.a2a.client.http.client.HTTPResponse", _Response)
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: sock,
        ssl_context=_TlsContext(),
    )

    client.cancel_task(
        endpoint_url="https://peer.example/a2a/v1",
        protocol_version="1.0",
        endpoint_tenant="acme/team",
        remote_task_id="remote/1",
        metadata={"reason": "operator request"},
    )

    request = sock.sent.decode()
    assert request.startswith(
        "POST /a2a/v1/acme%2Fteam/tasks/remote%2F1:cancel HTTP/1.1\r\n"
    )
    assert '"id":"remote/1"' in request
    assert '"tenant":"acme/team"' in request
    assert '"reason":"operator request"' in request


def test_client_fetches_standard_agent_card_with_conditional_cache_headers(monkeypatch) -> None:
    sock = _Socket()
    monkeypatch.setattr("agentmesh.integrations.a2a.client.http.client.HTTPResponse", _CardResponse)
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: sock,
        ssl_context=_TlsContext(),
    )
    result = client.fetch_agent_card(
        discovery_url="https://peer.example/.well-known/agent-card.json",
        source_etag='"card-v1"',
    )

    request = sock.sent.decode()
    assert request.startswith("GET /.well-known/agent-card.json HTTP/1.1\r\n")
    assert '\r\nIf-None-Match: "card-v1"\r\n' in request
    assert "Authorization:" not in request
    assert result.card == {"name": "Remote Agent"}
    assert result.source_etag == '"card-v2"'
    assert result.cache_max_age_seconds == 7200


def test_client_rejects_nonstandard_discovery_path_before_connecting() -> None:
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: _Socket(),
        ssl_context=_TlsContext(),
    )
    with pytest.raises(A2ATransportFailure, match="well-known") as caught:
        client.fetch_agent_card(discovery_url="https://peer.example/custom-card.json")
    assert not caught.value.request_may_have_been_sent


def test_client_accepts_not_modified_as_cache_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentmesh.integrations.a2a.client.http.client.HTTPResponse", _NotModifiedResponse
    )
    client = PinnedHttpsA2AClient(
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        socket_factory=lambda *args: _Socket(),
        ssl_context=_TlsContext(),
    )
    result = client.fetch_agent_card(
        discovery_url="https://peer.example/.well-known/agent-card.json",
        source_etag='"card-v1"',
    )
    assert result.not_modified
    assert result.card is None
    assert result.source_etag == '"card-v2"'
    assert result.cache_max_age_seconds == 7200
