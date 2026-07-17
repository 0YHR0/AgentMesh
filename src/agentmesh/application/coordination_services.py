from __future__ import annotations

from typing import Any

from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.errors import AgentUnavailable, InvalidTaskTransition
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentVersion,
    AgentVersionStatus,
    normalize_agent_name,
)
from agentmesh.domain.tasks import RunRole, Task, TaskRun, TaskStatus


class CoordinatedScheduler:
    """Deterministic, transaction-local scheduler for the bounded DAG slice."""

    def __init__(self, *, supervisor_agent_id: str) -> None:
        self._supervisor_agent_id = supervisor_agent_id

    def start(self, uow: Any, task: Task) -> list[TaskRun]:
        for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
            self._resolve_subtask_agent(uow, task.tenant_id, subtask)
        task.start_coordination()
        return self.schedule(uow, task)

    def schedule(self, uow: Any, task: Task) -> list[TaskRun]:
        if task.status != TaskStatus.RUNNING:
            return []
        subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        by_id = {subtask.id: subtask for subtask in subtasks}
        predecessors: dict[Any, set[Any]] = {subtask.id: set() for subtask in subtasks}
        for dependency in dependencies:
            predecessors[dependency.successor_id].add(dependency.predecessor_id)

        for subtask in subtasks:
            if subtask.status != SubtaskStatus.BLOCKED:
                continue
            if all(
                by_id[predecessor_id].status == SubtaskStatus.COMPLETED
                for predecessor_id in predecessors[subtask.id]
            ):
                subtask.mark_ready()
                uow.subtasks.save(subtask)

        if subtasks and all(
            subtask.status == SubtaskStatus.COMPLETED for subtask in subtasks
        ):
            if task.current_run_id is not None:
                return []
            agent_name, agent_version = self._resolve_named_agent(
                uow, task.tenant_id, self._supervisor_agent_id, {"general.supervise"}
            )
            run = TaskRun.request(
                task.id,
                agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=RunRole.SUPERVISOR,
            )
            task.queue_supervisor(run.id)
            self._persist_run_request(uow, task, run)
            return [run]

        active = sum(
            1
            for subtask in subtasks
            if subtask.current_run_id is not None
            and subtask.status in {SubtaskStatus.READY, SubtaskStatus.RUNNING}
        )
        available = max(task.max_concurrency - active, 0)
        created: list[TaskRun] = []
        for subtask in sorted(subtasks, key=lambda value: value.key):
            if available == 0:
                break
            if subtask.status != SubtaskStatus.READY or subtask.current_run_id is not None:
                continue
            agent_name, agent_version = self._resolve_subtask_agent(
                uow, task.tenant_id, subtask
            )
            run = TaskRun.request(
                task.id,
                agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=RunRole.EXECUTOR,
                subtask_id=subtask.id,
            )
            subtask.queue(run.id)
            uow.subtasks.save(subtask)
            self._persist_run_request(uow, task, run)
            created.append(run)
            available -= 1
        return created

    @staticmethod
    def work_item_input(uow: Any, task: Task, run: TaskRun) -> tuple[str, dict[str, Any]]:
        subtasks = uow.subtasks.list_for_task(task.id)
        if run.role == RunRole.SUPERVISOR:
            return (
                f"Synthesize coordinated result: {task.objective}",
                {
                    "plan_version": task.plan_version,
                    "plan_digest": task.plan_digest,
                    "subtask_outputs": {
                        subtask.key: dict(subtask.output or {}) for subtask in subtasks
                    },
                },
            )
        if run.subtask_id is None:
            raise InvalidTaskTransition(f"Coordinated Run {run.id} has no Subtask binding")
        by_id = {subtask.id: subtask for subtask in subtasks}
        subtask = by_id.get(run.subtask_id)
        if subtask is None:
            raise InvalidTaskTransition(f"Run {run.id} references an unknown Subtask")
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        predecessor_ids = {
            dependency.predecessor_id
            for dependency in dependencies
            if dependency.successor_id == subtask.id
        }
        return (
            subtask.objective,
            {
                "subtask_input": dict(subtask.input),
                "dependency_outputs": {
                    by_id[predecessor_id].key: dict(by_id[predecessor_id].output or {})
                    for predecessor_id in sorted(
                        predecessor_ids, key=lambda value: by_id[value].key
                    )
                },
            },
        )

    def _resolve_subtask_agent(
        self, uow: Any, tenant_id: str, subtask: Subtask
    ) -> tuple[str, AgentVersion]:
        required = set(subtask.required_capabilities)
        if subtask.preferred_agent_id is not None:
            return self._resolve_named_agent(
                uow, tenant_id, subtask.preferred_agent_id, required
            )
        candidates: list[tuple[str, AgentVersion]] = []
        definitions = uow.agent_definitions.list(tenant_id=tenant_id, limit=1_000, offset=0)
        for definition in definitions:
            if (
                definition.lifecycle != AgentDefinitionLifecycle.ACTIVE
                or definition.default_version_id is None
            ):
                continue
            version = uow.agent_versions.get(definition.default_version_id)
            if self._version_satisfies(version, required):
                candidates.append((definition.name, version))
        if not candidates:
            capabilities = ", ".join(sorted(required))
            raise AgentUnavailable(
                f"No published Agent Version satisfies Subtask {subtask.key}: {capabilities}"
            )
        return min(candidates, key=lambda value: (value[0], str(value[1].id)))

    @classmethod
    def _resolve_named_agent(
        cls,
        uow: Any,
        tenant_id: str,
        configured_name: str,
        required: set[str],
    ) -> tuple[str, AgentVersion]:
        name = normalize_agent_name(configured_name)
        definition = uow.agent_definitions.get_by_name(tenant_id, name, for_update=True)
        if (
            definition is None
            or definition.lifecycle != AgentDefinitionLifecycle.ACTIVE
            or definition.default_version_id is None
        ):
            raise AgentUnavailable(f"Agent {name} has no active published default version")
        version = uow.agent_versions.get(definition.default_version_id, for_update=True)
        if not cls._version_satisfies(version, required):
            capabilities = ", ".join(sorted(required))
            raise AgentUnavailable(f"Agent {name} does not satisfy capabilities: {capabilities}")
        return definition.name, version

    @staticmethod
    def _version_satisfies(version: AgentVersion | None, required: set[str]) -> bool:
        return bool(
            version is not None
            and version.status == AgentVersionStatus.PUBLISHED
            and version.content_digest
            and "async" in version.execution_modes
            and required.issubset(version.verified_capabilities)
        )

    @staticmethod
    def _persist_run_request(uow: Any, task: Task, run: TaskRun) -> None:
        uow.runs.add(run)
        uow.outbox.add(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
            )
        )
