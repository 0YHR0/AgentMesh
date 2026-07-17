from types import SimpleNamespace

from agentmesh.entrypoints.relay import run_relay_cycle


def test_retention_failure_does_not_stop_relay_publication() -> None:
    relay = _Relay()
    retention = _FailingRetention()
    metrics = _Metrics()
    container = SimpleNamespace(relay=relay, retention=retention)

    published = run_relay_cycle(container, metrics)  # type: ignore[arg-type]

    assert published == 2
    assert relay.calls == 1
    assert retention.calls == 1
    assert metrics.failures == 1


def test_metrics_failure_does_not_stop_relay_publication() -> None:
    relay = _Relay()
    container = SimpleNamespace(relay=relay, retention=_SuccessfulRetention())

    published = run_relay_cycle(  # type: ignore[arg-type]
        container,
        _FailingObserveMetrics(),  # type: ignore[arg-type]
    )

    assert published == 2
    assert relay.calls == 1


class _Relay:
    def __init__(self) -> None:
        self.calls = 0

    def publish_once(self) -> int:
        self.calls += 1
        return 2


class _FailingRetention:
    def __init__(self) -> None:
        self.calls = 0

    def run_if_due(self) -> None:
        self.calls += 1
        raise RuntimeError("retention is unavailable")


class _Metrics:
    def __init__(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1


class _Report:
    def to_dict(self) -> dict[str, str]:
        return {"status": "ok"}


class _SuccessfulRetention:
    def run_if_due(self) -> _Report:
        return _Report()


class _FailingObserveMetrics:
    def observe(self, _report: _Report) -> None:
        raise RuntimeError("metrics unavailable")
