from __future__ import annotations

from agentmesh.application.a2a_delegation_services import (
    A2ADelegationService,
    A2AReconciliationReport,
)


class A2AReconciliationWorker:
    def __init__(
        self,
        *,
        service: A2ADelegationService,
        worker_id: str,
        batch_size: int,
    ) -> None:
        self._service = service
        self._worker_id = worker_id
        self._batch_size = batch_size

    def run_once(self) -> A2AReconciliationReport:
        return self._service.reconcile_due(
            worker_id=self._worker_id,
            limit=self._batch_size,
        )
