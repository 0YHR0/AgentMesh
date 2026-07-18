from scripts.ci import compose_e2e


def test_readiness_retries_connection_reset(monkeypatch) -> None:
    outcomes = iter([ConnectionResetError("container is still starting"), {"status": "ready"}])

    def request_json(_path: str):
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(compose_e2e, "request_json", request_json)
    monkeypatch.setattr(compose_e2e.time, "sleep", lambda _seconds: None)

    compose_e2e.wait_until_ready(timeout_seconds=1)


def test_metrics_wait_retries_connection_reset(monkeypatch) -> None:
    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self._payload

    outcomes = iter(
        [
            ConnectionResetError("container is still starting"),
            _Response(b"agentmesh_messaging_retention_last_success_timestamp_seconds 0\n"),
            _Response(b"agentmesh_messaging_retention_last_success_timestamp_seconds 1\n"),
        ]
    )

    def urlopen(_url: str, timeout: int):
        del timeout
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(compose_e2e, "urlopen", urlopen)
    monkeypatch.setattr(compose_e2e.time, "sleep", lambda _seconds: None)

    compose_e2e.wait_for_relay_metrics(timeout_seconds=1)
