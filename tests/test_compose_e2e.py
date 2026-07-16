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
