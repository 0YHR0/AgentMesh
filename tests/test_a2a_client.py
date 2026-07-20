import json
import ssl

import pytest

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
    assert sock.closed
