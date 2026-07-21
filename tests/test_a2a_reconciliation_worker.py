from agentmesh.application.a2a_delegation_services import A2AReconciliationReport
from agentmesh.workers.a2a_reconciliation import A2AReconciliationWorker


class _Service:
    def __init__(self) -> None:
        self.calls = []

    def reconcile_due(self, *, worker_id: str, limit: int) -> A2AReconciliationReport:
        self.calls.append((worker_id, limit))
        return A2AReconciliationReport(2, 1, 1, 0, 0)


def test_worker_runs_one_bounded_reconciliation_batch() -> None:
    service = _Service()
    worker = A2AReconciliationWorker(
        service=service,
        worker_id="a2a-worker-1",
        batch_size=20,
    )
    report = worker.run_once()
    assert report.claimed == 2
    assert service.calls == [("a2a-worker-1", 20)]
