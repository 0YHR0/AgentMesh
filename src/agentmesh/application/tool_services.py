from __future__ import annotations

from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import TaskNotFound, ToolInvocationFailed, ToolOutcomeUnknown
from agentmesh.domain.tools import (
    ToolAuthorizationStatus,
    ToolBinding,
    ToolCallResult,
    ToolExecutionAuthorization,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSideEffect,
)


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
            if binding.side_effect is not ToolSideEffect.READ_ONLY:
                authorization = uow.tool_execution_authorizations.get_for_task(
                    task.id, for_update=True
                )
                if authorization is None:
                    raise ToolInvocationFailed("MCP write Task has no durable authorization")
                if authorization.status is ToolAuthorizationStatus.EXECUTING:
                    if authorization.invocation_id is None:
                        raise ToolInvocationFailed("MCP write authorization linkage was lost")
                    previous = uow.tool_invocations.get(
                        authorization.invocation_id, for_update=True
                    )
                    if previous is None or previous.status is not ToolInvocationStatus.RUNNING:
                        raise ToolInvocationFailed("MCP write invocation linkage was lost")
                    previous.outcome_unknown(
                        "MCP write was interrupted before its outcome was persisted"
                    )
                    uow.tool_invocations.save(previous)
                    authorization.settle(previous.status)
                    uow.tool_execution_authorizations.save(authorization)
                    uow.commit()
                    raise ToolOutcomeUnknown(
                        "Previous MCP write delivery outcome is unknown; automatic replay stopped"
                    )
                uow.tool_invocations.add(invocation)
                uow.flush()
                authorization.claim(
                    invocation_id=invocation.id,
                    binding=binding,
                    arguments=arguments,
                )
                uow.tool_execution_authorizations.save(authorization)
            else:
                uow.tool_invocations.add(invocation)
            uow.commit()
        return invocation

    def succeed(self, invocation_id: UUID, result: ToolCallResult) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.succeed(result)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def fail(self, invocation_id: UUID, error: str) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.fail(error)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def outcome_unknown(self, invocation_id: UUID, error: str) -> ToolInvocation:
        with self._uow_factory() as uow:
            invocation = self._get_owned_or_raise(uow, invocation_id, for_update=True)
            invocation.outcome_unknown(error)
            uow.tool_invocations.save(invocation)
            self._settle_authorization(uow, invocation)
            uow.commit()
            return invocation

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            return uow.tool_invocations.list_for_task(task.id)

    def audit_for_task(
        self, task_id: UUID
    ) -> tuple[ToolExecutionAuthorization | None, list[ToolInvocation]]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            return (
                uow.tool_execution_authorizations.get_for_task(task.id),
                uow.tool_invocations.list_for_task(task.id),
            )

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

    @staticmethod
    def _settle_authorization(uow: Any, invocation: ToolInvocation) -> None:
        if invocation.side_effect is ToolSideEffect.READ_ONLY:
            return
        authorization = uow.tool_execution_authorizations.get_for_task(
            invocation.task_id, for_update=True
        )
        if authorization is None or authorization.invocation_id != invocation.id:
            raise ToolInvocationFailed("MCP write authorization linkage was lost")
        assert invocation.status is not ToolInvocationStatus.RUNNING
        authorization.settle(invocation.status)
        uow.tool_execution_authorizations.save(authorization)
