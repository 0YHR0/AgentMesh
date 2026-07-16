from __future__ import annotations

from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import TaskNotFound, ToolInvocationFailed
from agentmesh.domain.tools import ToolBinding, ToolCallResult, ToolInvocation


class ToolInvocationService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def start(
        self,
        *,
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolInvocation:
        invocation = ToolInvocation.start(
            tenant_id=self._tenant_id,
            task_id=task_id,
            run_id=run_id,
            binding=binding,
            arguments=arguments,
        )
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            run = uow.runs.get(run_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            if run is None or run.task_id != task.id:
                raise ToolInvocationFailed("Tool Invocation references an unavailable Run")
            uow.tool_invocations.add(invocation)
            uow.commit()
        return invocation

    def succeed(self, invocation_id: UUID, result: ToolCallResult) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.succeed(result)
            uow.tool_invocations.save(invocation)
            uow.commit()
            return invocation

    def fail(self, invocation_id: UUID, error: str) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.fail(error)
            uow.tool_invocations.save(invocation)
            uow.commit()
            return invocation

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            return uow.tool_invocations.list_for_task(task.id)

    def _get_owned_or_raise(
        self,
        uow: Any,
        invocation_id: UUID,
        *,
        for_update: bool,
    ) -> ToolInvocation:
        invocation = uow.tool_invocations.get(invocation_id, for_update=for_update)
        if invocation is None or invocation.tenant_id != self._tenant_id:
            raise ToolInvocationFailed(f"Tool Invocation {invocation_id} was not found")
        return invocation
