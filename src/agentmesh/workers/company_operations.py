from agentmesh.application.company_operation_services import (
    CompanyOperationService,
    OperationLaunch,
)


class CompanyOperationsWorker:
    def __init__(
        self, *, service: CompanyOperationService, batch_size: int
    ) -> None:
        self._service = service
        self._batch_size = batch_size

    def run_once(self) -> list[OperationLaunch]:
        return self._service.dispatch_due(limit=self._batch_size)
