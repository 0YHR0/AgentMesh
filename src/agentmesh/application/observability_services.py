from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import TaskNotFound
from agentmesh.domain.observability import TaskUsage


class UsageQueryService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def get_task_usage(self, task_id: UUID) -> TaskUsage:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            records = uow.usage_records.list_for_task(task_id)
            return TaskUsage.summarize(task_id, records)
