from __future__ import annotations

from datetime import datetime
from typing import Any

from agentmesh.application.agent_resolution import (
    resolve_capable_agent as resolve_agent_by_name,
)
from agentmesh.application.agent_resolution import (
    version_satisfies as agent_version_satisfies,
)
from agentmesh.application.authority_cohorts import (
    AuthorityCohort,
    AuthorityCohortResolver,
    ContinuationKind,
)
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.errors import AgentUnavailable, InvalidTaskTransition
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentVersion,
)
from agentmesh.domain.tasks import RunRole, Task, TaskRun, TaskStatus
from agentmesh.features import FeatureGateSet


class CoordinatedScheduler:
    """Deterministic, transaction-local scheduler for the bounded DAG slice."""

    def __init__(
        self,
        *,
        supervisor_agent_id: str,
        authority_cohort_resolver: AuthorityCohortResolver | None = None,
    ) -> None:
        self._supervisor_agent_id = supervisor_agent_id
        self._authority_cohort_resolver = authority_cohort_resolver or AuthorityCohortResolver(
            feature_gates=FeatureGateSet.from_config("minimal")
        )

    def start(self, uow: Any, task: Task, *, at: datetime | None = None) -> list[TaskRun]:
        accepted_by_target = self._accepted_by_target(uow, task.id)
        for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
            self._resolve_subtask_agent(
                uow, task.tenant_id, subtask, accepted_by_target.get(subtask.id)
            )
        task.start_coordination(at=at)
        return self.schedule(uow, task, at=at)

    def schedule(self, uow: Any, task: Task, *, at: datetime | None = None) -> list[TaskRun]:
        if task.status != TaskStatus.RUNNING:
            return []
        subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
        accepted_by_target = self._accepted_by_target(uow, task.id)
        dependencies = uow.subtask_dependencies.list_for_task(task.id)
        cohort = self._authority_cohort_resolver.resolve_continuation_cohort_in_uow(uow, task)
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
                subtask.mark_ready(at=at)
                uow.subtasks.save(subtask)

        if subtasks and all(subtask.status == SubtaskStatus.COMPLETED for subtask in subtasks):
            if task.current_run_id is not None:
                return []
            rejection = BudgetController.run_rejection(uow, task, now=at)
            if rejection is not None:
                task.wait_for_budget(rejection, at=at)
                return []
            agent_name, agent_version = self.resolve_named_agent(
                uow, task.tenant_id, self._supervisor_agent_id, {"general.supervise"}
            )
            run = self._new_run(
                uow,
                task,
                agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=RunRole.SUPERVISOR,
                cohort=cohort,
                kind=ContinuationKind.COORDINATED,
                at=at,
            )
            task.queue_supervisor(run.id, at=at)
            self._persist_run_request(uow, task, run, at=at)
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
            rejection = BudgetController.run_rejection(uow, task, now=at)
            if rejection is not None:
                if active == 0 and not created:
                    task.wait_for_budget(rejection, at=at)
                break
            agent_name, agent_version = self._resolve_subtask_agent(
                uow,
                task.tenant_id,
                subtask,
                accepted_by_target.get(subtask.id),
            )
            run = self._new_run(
                uow,
                task,
                agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=RunRole.EXECUTOR,
                subtask_id=subtask.id,
                cohort=cohort,
                kind=ContinuationKind.COORDINATED,
                at=at,
            )
            subtask.queue(run.id, at=at)
            uow.subtasks.save(subtask)
            self._persist_run_request(uow, task, run, at=at)
            uow.flush()
            created.append(run)
            available -= 1
        return created

    def _new_run(
        self,
        uow: Any,
        task: Task,
        agent_id: str,
        *,
        cohort: AuthorityCohort,
        at: datetime | None = None,
        **kwargs: Any,
    ) -> TaskRun:
        return self._authority_cohort_resolver.create_continuation_from_cohort_in_uow(
            uow, task, agent_id=agent_id, cohort=cohort, at=at, **kwargs
        )

    def work_item_input(self, uow: Any, task: Task, run: TaskRun) -> tuple[str, dict[str, Any]]:
        item = CanonicalWorkItemBuilder(self).build(task, run, uow=uow)
        return item.objective, item.input

    @staticmethod
    def _work_item_input_legacy(uow: Any, task: Task, run: TaskRun) -> tuple[str, dict[str, Any]]:
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
                "accepted_handoffs": [
                    handoff.execution_context()
                    for handoff in uow.handoffs.list_for_target(
                        subtask.id, status=HandoffStatus.ACCEPTED
                    )
                ],
            },
        )

    def _resolve_subtask_agent(
        self,
        uow: Any,
        tenant_id: str,
        subtask: Subtask,
        accepted_handoff: Handoff | None = None,
    ) -> tuple[str, AgentVersion]:
        required = set(subtask.required_capabilities)
        if accepted_handoff is not None:
            return self.resolve_named_agent(
                uow, tenant_id, accepted_handoff.target_agent_id, required
            )
        if subtask.preferred_agent_id is not None:
            return self.resolve_named_agent(uow, tenant_id, subtask.preferred_agent_id, required)
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

    @staticmethod
    def _accepted_by_target(uow: Any, task_id: Any) -> dict[Any, Handoff]:
        return {
            handoff.target_subtask_id: handoff
            for handoff in uow.handoffs.list_for_task(task_id)
            if handoff.status == HandoffStatus.ACCEPTED
        }

    @classmethod
    def resolve_named_agent(
        cls,
        uow: Any,
        tenant_id: str,
        configured_name: str,
        required: set[str],
    ) -> tuple[str, AgentVersion]:
        return resolve_agent_by_name(uow, tenant_id, configured_name, required)

    @staticmethod
    def _version_satisfies(version: AgentVersion | None, required: set[str]) -> bool:
        return agent_version_satisfies(version, required)

    @staticmethod
    def _persist_run_request(
        uow: Any, task: Task, run: TaskRun, *, at: datetime | None = None
    ) -> None:
        uow.runs.add(run)
        uow.outbox.add(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
                at=at,
            )
        )
