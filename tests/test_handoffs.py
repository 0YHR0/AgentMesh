from __future__ import annotations

from uuid import UUID

import pytest

from agentmesh.application.handoff_services import HandoffApplicationService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import (
    FeatureDisabled,
    IdempotencyConflict,
    InvalidTaskInput,
)
from agentmesh.domain.handoffs import HandoffStatus
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.tasks import RunRole, TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _spec(key: str, *, depends_on: tuple[str, ...] = ()) -> SubtaskSpec:
    return SubtaskSpec.create(
        key=key,
        objective=f"Execute {key}",
        input={"key": key},
        depends_on=depends_on,
    )


def _wakeup(uow_factory: InMemoryUnitOfWorkFactory, run_id):
    return next(
        envelope
        for envelope in reversed(uow_factory.store.outbox)
        if envelope.schema_name == RUN_REQUESTED_SCHEMA
        and envelope.payload["run_id"] == str(run_id)
    )


def test_accepted_handoff_binds_target_agent_and_enters_context(
    task_service: TaskApplicationService,
    handoff_service: HandoffApplicationService,
    execution_service: RunExecutionService,
    registry_service: AgentRegistryService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create(
        (
            _spec("source"),
            _spec("other"),
            _spec("target", depends_on=("source", "other")),
        ),
        max_concurrency=2,
    )
    created = task_service.create_task(
        "Exercise a structured Handoff",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(created.task.id)
    subtasks = {value.key: value for value in started.subtasks}
    source_run = next(
        run for run in started.runs if run.subtask_id == subtasks["source"].id
    )
    other_run = next(
        run for run in started.runs if run.subtask_id == subtasks["other"].id
    )
    assert execution_service.process(_wakeup(uow_factory, source_run.id)) is True

    registry_service.ensure_builtin_agent("z-handoff-agent")
    requested = handoff_service.request_handoff(
        task_id=created.task.id,
        source_subtask_id=subtasks["source"].id,
        target_subtask_id=subtasks["target"].id,
        target_agent_id="z-handoff-agent",
        objective="Finish the joined analysis",
        reason="A specialist should own synthesis",
        completed_work_summary="Source evidence is complete",
        requested_by=source_run.agent_id,
        unresolved_questions=("How should both branches be weighted?",),
        constraints={"format": "json"},
        acceptance_criteria=(
            {"key": "joined", "description": "Both branches are joined", "required": True},
        ),
        idempotency_key="request-1",
    )
    replay = handoff_service.request_handoff(
        task_id=created.task.id,
        source_subtask_id=subtasks["source"].id,
        target_subtask_id=subtasks["target"].id,
        target_agent_id="z-handoff-agent",
        objective="Finish the joined analysis",
        reason="A specialist should own synthesis",
        completed_work_summary="Source evidence is complete",
        requested_by=source_run.agent_id,
        unresolved_questions=("How should both branches be weighted?",),
        constraints={"format": "json"},
        acceptance_criteria=(
            {"key": "joined", "description": "Both branches are joined", "required": True},
        ),
        idempotency_key="request-1",
    )
    assert replay.id == requested.id
    assert requested.status == HandoffStatus.REQUESTED

    accepted = handoff_service.accept_handoff(
        created.task.id,
        requested.id,
        actor="z-handoff-agent",
        reason="Capability and context are sufficient",
        idempotency_key="accept-1",
    )
    accepted_replay = handoff_service.accept_handoff(
        created.task.id,
        requested.id,
        actor="z-handoff-agent",
        reason="Capability and context are sufficient",
        idempotency_key="accept-1",
    )
    assert accepted_replay.id == accepted.id
    assert accepted.status == HandoffStatus.ACCEPTED
    accepted_event_count = sum(
        item.schema_name == "agentmesh.handoff.accepted"
        for item in uow_factory.store.outbox
    )
    terminal_replay = handoff_service.accept_handoff(
        created.task.id,
        requested.id,
        actor="z-handoff-agent",
        reason="Capability and context are sufficient",
    )
    assert terminal_replay.id == accepted.id
    assert (
        sum(
            item.schema_name == "agentmesh.handoff.accepted"
            for item in uow_factory.store.outbox
        )
        == accepted_event_count
    )

    assert execution_service.process(_wakeup(uow_factory, other_run.id)) is True
    with_target = task_service.get_task(created.task.id)
    target_run = next(
        run for run in with_target.runs if run.subtask_id == subtasks["target"].id
    )
    assert target_run.agent_id == "z-handoff-agent"
    scheduled_replay = handoff_service.accept_handoff(
        created.task.id,
        requested.id,
        actor="z-handoff-agent",
        reason="Capability and context are sufficient",
    )
    assert scheduled_replay.id == accepted.id
    assert execution_service.process(_wakeup(uow_factory, target_run.id)) is True

    awaiting_supervisor = task_service.get_task(created.task.id)
    completed_target = next(
        value for value in awaiting_supervisor.subtasks if value.key == "target"
    )
    handoff_context = completed_target.output["input"]["accepted_handoffs"]
    assert handoff_context == [accepted.execution_context()]
    assert awaiting_supervisor.handoffs[0].status == HandoffStatus.ACCEPTED
    supervisor = next(
        run for run in awaiting_supervisor.runs if run.role == RunRole.SUPERVISOR
    )
    assert execution_service.process(_wakeup(uow_factory, supervisor.id)) is True
    assert task_service.get_task(created.task.id).task.status == TaskStatus.COMPLETED


def test_rejected_handoff_does_not_change_target_assignment(
    task_service: TaskApplicationService,
    handoff_service: HandoffApplicationService,
    execution_service: RunExecutionService,
    registry_service: AgentRegistryService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create(
        (
            _spec("source"),
            _spec("other"),
            _spec("target", depends_on=("source", "other")),
        ),
        max_concurrency=2,
    )
    task = task_service.create_task(
        "Reject a Handoff",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(task.task.id)
    subtasks = {value.key: value for value in started.subtasks}
    source_run = next(run for run in started.runs if run.subtask_id == subtasks["source"].id)
    other_run = next(run for run in started.runs if run.subtask_id == subtasks["other"].id)
    execution_service.process(_wakeup(uow_factory, source_run.id))
    registry_service.ensure_builtin_agent("z-handoff-agent")
    handoff = handoff_service.request_handoff(
        task_id=task.task.id,
        source_subtask_id=subtasks["source"].id,
        target_subtask_id=subtasks["target"].id,
        target_agent_id="z-handoff-agent",
        objective="Take over target",
        reason="Specialization",
        completed_work_summary="Source done",
        requested_by=source_run.agent_id,
    )
    rejected = handoff_service.reject_handoff(
        task.task.id,
        handoff.id,
        actor="z-handoff-agent",
        reason="Insufficient context",
    )
    assert rejected.status == HandoffStatus.REJECTED

    execution_service.process(_wakeup(uow_factory, other_run.id))
    target_run = next(
        run
        for run in task_service.get_task(task.task.id).runs
        if run.subtask_id == subtasks["target"].id
    )
    assert target_run.agent_id == "test-agent"


def test_handoff_rejects_invalid_relationship_actor_and_idempotency_conflict(
    task_service: TaskApplicationService,
    handoff_service: HandoffApplicationService,
    execution_service: RunExecutionService,
    registry_service: AgentRegistryService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create(
        (
            _spec("source"),
            _spec("other"),
            _spec("target", depends_on=("source", "other")),
            _spec("unrelated", depends_on=("other",)),
        ),
        max_concurrency=2,
    )
    task = task_service.create_task(
        "Validate Handoff boundaries",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(task.task.id)
    subtasks = {value.key: value for value in started.subtasks}
    source_run = next(
        run for run in started.runs if run.subtask_id == subtasks["source"].id
    )
    execution_service.process(_wakeup(uow_factory, source_run.id))
    registry_service.ensure_builtin_agent("z-handoff-agent")

    with_invalid_target = dict(
        task_id=task.task.id,
        source_subtask_id=subtasks["source"].id,
        target_subtask_id=subtasks["target"].id,
        target_agent_id="z-handoff-agent",
        objective="Take target",
        reason="Specialization",
        completed_work_summary="Source done",
        requested_by=source_run.agent_id,
        idempotency_key="same-key",
    )
    handoff = handoff_service.request_handoff(**with_invalid_target)
    with pytest.raises(IdempotencyConflict):
        handoff_service.request_handoff(
            **{**with_invalid_target, "reason": "A conflicting reason"}
        )
    with pytest.raises(InvalidTaskInput, match="decision actor"):
        handoff_service.accept_handoff(
            task.task.id, handoff.id, actor="test-agent"
        )
    with pytest.raises(InvalidTaskInput, match="downstream"):
        handoff_service.request_handoff(
            task_id=task.task.id,
            source_subtask_id=subtasks["source"].id,
            target_subtask_id=subtasks["unrelated"].id,
            target_agent_id="z-handoff-agent",
            objective="Take unrelated work",
            reason="Invalid edge",
            completed_work_summary="Source done",
            requested_by=source_run.agent_id,
        )
    for index in range(7):
        handoff_service.request_handoff(
            task_id=task.task.id,
            source_subtask_id=subtasks["source"].id,
            target_subtask_id=subtasks["target"].id,
            target_agent_id="z-handoff-agent",
            objective="Take target",
            reason=f"Bounded request {index}",
            completed_work_summary="Source done",
            requested_by=source_run.agent_id,
        )
    with pytest.raises(InvalidTaskInput, match="at most 8"):
        handoff_service.request_handoff(
            task_id=task.task.id,
            source_subtask_id=subtasks["source"].id,
            target_subtask_id=subtasks["target"].id,
            target_agent_id="z-handoff-agent",
            objective="Exceed the bound",
            reason="One request too many",
            completed_work_summary="Source done",
            requested_by=source_run.agent_id,
        )


def test_handoffs_are_disabled_without_feature_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = HandoffApplicationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        supervisor_agent_id="test-supervisor",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with pytest.raises(FeatureDisabled):
        service.get_handoff(UUID(int=1), UUID(int=2))
